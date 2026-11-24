"""Streaming chat endpoint (Starlette) run on Lambda via the Lambda Web Adapter.

Request path:  CloudFront /api/* -> API Gateway REST (Cognito authorizer,
throttling, request validation, responseTransferMode=STREAM) -> this function.

The response is Server-Sent Events over a single HTTP response:

    event: meta        {"conversationId", "modelId"}
    event: text        {"delta"}
    event: tool_use    {"id", "name", "input"}
    event: tool_result {"id", "name", "ok", "ms", "citations"}
    event: warning     {"message"}
    event: error       {"message", "code"}
    event: done        {"usage", "stopReason", "iterations", "toolCalls"}
"""

from __future__ import annotations

import json
import re
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from agent_loop import AgentEvent, AgentLoop
from settings import AgentSettings
from tool_gateway import McpToolGateway, ToolGateway

from shared.auth import Caller, UnauthorizedError, caller_from_claims, caller_from_lwa_header
from shared.conversations import ConversationStore
from shared.model_catalog import ModelCatalog, catalog_from_env

MAX_BODY_BYTES = 16 * 1024

try:  # Powertools is present in the Lambda bundle; tests run without metrics output.
    from aws_lambda_powertools import Logger, Metrics
    from aws_lambda_powertools.metrics import MetricUnit

    logger: Any = Logger(service=os.environ.get("POWERTOOLS_SERVICE_NAME", "agent"))
    metrics: Any = Metrics()
except Exception:  # noqa: BLE001  # pragma: no cover
    logger = logging.getLogger("agent")
    metrics = None
    MetricUnit = None  # type: ignore[assignment]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    model_id: str | None = Field(default=None, alias="modelId", max_length=200)
    conversation_id: str | None = Field(default=None, alias="conversationId")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("conversation_id")
    @classmethod
    def _uuid(cls, v: str | None) -> str | None:
        if v is not None:
            uuid.UUID(v)
        return v

    @field_validator("message")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message is empty")
        return v


@dataclass
class Deps:
    catalog: ModelCatalog
    store: ConversationStore
    bedrock: Any
    tools: ToolGateway | None
    settings: AgentSettings


_deps: Deps | None = None


def default_deps() -> Deps:
    global _deps
    if _deps is None:
        import boto3
        from botocore.config import Config

        settings = AgentSettings.from_env()
        bedrock = boto3.client(
            "bedrock-runtime",
            region_name=settings.region,
            config=Config(retries={"mode": "adaptive", "max_attempts": 3}, connect_timeout=5, read_timeout=60),
        )
        tools = (
            McpToolGateway(settings.mcp_url, settings.region, settings.tool_cache_ttl_s, settings.tool_timeout_s)
            if settings.mcp_url
            else None
        )
        _deps = Deps(catalog_from_env(), ConversationStore(), bedrock, tools, settings)
    return _deps


def _caller(request: Request) -> Caller:
    if os.environ.get("AUTH_MODE") == "local-dev" and not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return caller_from_claims({"sub": os.environ.get("LOCAL_DEV_USER", "local-dev-user"), "cognito:groups": "admin"})
    return caller_from_lwa_header(request.headers.get("x-amzn-request-context"))


def _sse(event: AgentEvent | tuple[str, dict]) -> bytes:
    etype, data = (event.type, event.data) if isinstance(event, AgentEvent) else event
    return f"event: {etype}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n".encode()


_ARN_RE = re.compile(r"arn:aws[a-zA-Z-]*:[^\s\"',;)]*")
_ACCOUNT_RE = re.compile(r"\b\d{12}\b")
_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_PUBLIC_ERROR_LIMIT = 300


def public_error(exc: BaseException) -> tuple[str, str]:
    """Return ``(code, message)`` that is safe and useful to show in the browser.

    AWS service errors (``ClientError``, including errors raised from inside an
    event stream) carry a message that explains what was wrong with the request,
    e.g. which field Bedrock rejected. We pass that through after removing ARNs,
    account ids and access-key ids, and cap the length. Anything else is an
    internal bug and gets a generic message; details stay in the logs.
    """
    error = (getattr(exc, "response", None) or {}).get("Error", {})
    code = str(error.get("Code") or type(exc).__name__)
    raw = error.get("Message")
    if not raw:
        return code, "The model call failed. Please retry."
    msg = _KEY_RE.sub("<redacted>", _ACCOUNT_RE.sub("<account>", _ARN_RE.sub("<arn>", str(raw))))
    msg = " ".join(msg.split())
    if len(msg) > _PUBLIC_ERROR_LIMIT:
        msg = msg[: _PUBLIC_ERROR_LIMIT - 1] + "…"
    return code, msg


def _emit_metric(name: str, value: float, _dims: dict) -> None:
    if metrics is None:
        return
    unit = MetricUnit.Milliseconds if name.endswith("Ms") else MetricUnit.Count
    metrics.add_metric(name=name, unit=unit, value=value)


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def chat(request: Request):
    deps: Deps = request.app.state.deps_factory()
    try:
        caller = _caller(request)
    except UnauthorizedError:
        return JSONResponse({"message": "Unauthorized"}, status_code=401)

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"message": "Request too large"}, status_code=413)
    try:
        req = ChatRequest.model_validate_json(body)
        spec = deps.catalog.resolve(req.model_id)
    except ValidationError as exc:
        return JSONResponse({"message": "Invalid request", "errors": json.loads(exc.json(include_url=False))}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"message": str(exc)}, status_code=400)

    history: list[dict] = []
    conversation_id = req.conversation_id
    if conversation_id:
        existing = deps.store.get(caller.user_id, conversation_id)
        if existing is None:
            return JSONResponse({"message": "Conversation not found"}, status_code=404)
        history = existing["messages"]
    else:
        conversation_id = ConversationStore.new_id()

    loop = AgentLoop(deps.bedrock, deps.tools, deps.settings, on_metric=_emit_metric)
    request_id = request.headers.get("x-amzn-request-id") or str(uuid.uuid4())

    async def stream() -> AsyncIterator[bytes]:
        started = time.perf_counter()
        yield _sse(("meta", {"conversationId": conversation_id, "modelId": spec.id, "requestId": request_id}))
        try:
            async for event in loop.run(spec, history, req.message):
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001 - always end the stream with a typed error
            code, public_message = public_error(exc)
            logger.exception("agent failed", extra={"error_code": code, "error_message": str(exc), "model": spec.id})
            _emit_metric("AgentErrors", 1, {})
            yield _sse(("error", {"message": public_message, "code": code}))
        finally:
            if loop.outcome is not None:
                try:
                    deps.store.save(caller.user_id, conversation_id, loop.outcome.messages, spec.id, title=req.message[:60])
                except Exception:  # noqa: BLE001
                    logger.exception("failed to persist conversation")
            _emit_metric("ChatLatencyMs", (time.perf_counter() - started) * 1000, {})
            if metrics is not None:
                metrics.flush_metrics()
            logger.info(
                "chat complete",
                extra={
                    "conversation_id": conversation_id,
                    "model": spec.id,
                    "iterations": getattr(loop.outcome, "iterations", None),
                    "tool_calls": getattr(loop.outcome, "tool_calls", None),
                    "usage": getattr(loop.outcome, "usage", None),
                },
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


def create_app(deps_factory=default_deps) -> Starlette:
    app = Starlette(routes=[Route("/healthz", healthz), Route("/api/chat", chat, methods=["POST"])])
    app.state.deps_factory = deps_factory
    return app


app = create_app()
