"""The agent loop: Bedrock Converse (streaming) + MCP tools.

    user message
        -> ConverseStream(model, history, toolConfig)
        -> text deltas are forwarded to the browser as they arrive
        -> stopReason == "tool_use": call the requested MCP tools (in parallel,
           each with a timeout), append toolResult blocks, call the model again
        -> stopReason == "end_turn" | "max_tokens" | "guardrail_intervened": done

Hard limits keep cost and latency bounded no matter what the model does:
``max_iterations`` model calls, a wall-clock ``request_deadline_s``, per-tool
timeouts and a cap on how much tool output is fed back to the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import AsyncIterator, Callable

from settings import SYSTEM_PROMPT, AgentSettings
from tool_gateway import ToolGateway, ToolResult

from shared.conversations import strip_tool_blocks
from shared.model_catalog import ModelSpec
from shared.retry import call_with_backoff

log = logging.getLogger(__name__)

_SENTINEL = object()


@dataclass
class AgentEvent:
    type: str
    data: dict = field(default_factory=dict)


@dataclass
class AgentOutcome:
    messages: list[dict]
    usage: dict
    stop_reason: str | None
    iterations: int
    tool_calls: int


def close_dangling_turns(messages: list[dict]) -> None:
    """Leave history in a state Converse accepts on the *next* turn.

    Converse needs user/assistant alternation and a toolResult for every toolUse.
    If the loop stopped early (iteration cap, deadline, tools down, empty model
    reply) we close the open turn with explicit placeholders.
    """
    if messages and messages[-1]["role"] == "assistant":
        pending = [b["toolUse"] for b in messages[-1]["content"] if "toolUse" in b]
        if pending:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"toolResult": {"toolUseId": t["toolUseId"], "content": [{"text": "not executed"}], "status": "error"}}
                        for t in pending
                    ],
                }
            )
    if messages and messages[-1]["role"] == "user":
        messages.append({"role": "assistant", "content": [{"text": "(Stopped before completing an answer.)"}]})


class AgentLoop:
    def __init__(
        self,
        bedrock_client: Any,
        tools: ToolGateway | None,
        settings: AgentSettings,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_metric: Callable[[str, float, dict], None] | None = None,
    ):
        self.bedrock = bedrock_client
        self.tools = tools
        self.settings = settings
        self.clock = clock
        self.sleep = sleep
        self.on_metric = on_metric or (lambda *_: None)
        self.outcome: AgentOutcome | None = None

    # ------------------------------------------------------------------ helpers
    def _request(self, spec: ModelSpec, messages: list[dict], tool_specs: list[dict]) -> dict:
        req: dict[str, Any] = {
            "modelId": spec.id,
            "messages": messages,
            "system": [{"text": SYSTEM_PROMPT}],
            "inferenceConfig": {"maxTokens": spec.max_tokens, "temperature": spec.temperature},
        }
        if tool_specs:
            req["toolConfig"] = {"tools": tool_specs}
        if self.settings.guardrail_id and self.settings.guardrail_version:
            req["guardrailConfig"] = {
                "guardrailIdentifier": self.settings.guardrail_id,
                "guardrailVersion": self.settings.guardrail_version,
                "streamProcessingMode": "async",
            }
        return req

    def _open_stream(self, request: dict):
        def _retry_log(attempt: int, exc: BaseException, delay: float) -> None:
            log.warning("bedrock throttled, retrying", extra={"attempt": attempt, "delay_s": round(delay, 2), "error": str(exc)})
            self.on_metric("BedrockRetries", 1, {"model": request["modelId"]})

        resp = call_with_backoff(lambda: self.bedrock.converse_stream(**request), sleep=self.sleep, on_retry=_retry_log)
        return iter(resp["stream"])

    @staticmethod
    async def _aiter(sync_iter) -> AsyncIterator[dict]:
        """Pull a blocking botocore event stream without blocking the event loop."""
        while True:
            item = await asyncio.to_thread(next, sync_iter, _SENTINEL)
            if item is _SENTINEL:
                return
            yield item

    async def _run_tool(self, session, tool_use: dict) -> tuple[dict, ToolResult, float]:
        started = time.perf_counter()
        try:
            if not isinstance(tool_use.get("input"), dict):
                result = ToolResult(False, "tool input must be a JSON object")
            else:
                result = await asyncio.wait_for(session.call_tool(tool_use["name"], tool_use["input"]), self.settings.tool_timeout_s)
        except TimeoutError:
            result = ToolResult(False, f"tool '{tool_use['name']}' timed out after {self.settings.tool_timeout_s}s")
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
            log.exception("tool call failed", extra={"tool": tool_use.get("name")})
            result = ToolResult(False, f"tool '{tool_use['name']}' failed: {type(exc).__name__}")
        return tool_use, result, (time.perf_counter() - started) * 1000

    # --------------------------------------------------------------------- main
    async def run(self, spec: ModelSpec, history: list[dict], user_text: str) -> AsyncIterator[AgentEvent]:
        started = self.clock()
        usage = {"inputTokens": 0, "outputTokens": 0}
        messages = list(history) + [{"role": "user", "content": [{"text": user_text}]}]
        stop_reason: str | None = None
        iterations = tool_calls = 0

        session_cm = self.tools.open() if (self.tools and spec.supports_tools) else None
        entered = False
        session = None
        tool_specs: list[dict] = []
        try:
            if session_cm is not None:
                try:
                    session = await session_cm.__aenter__()
                    entered = True
                    tool_specs = await session.list_tool_specs()
                except Exception as exc:  # noqa: BLE001 - degrade to a tool-less answer
                    log.exception("tool discovery failed; continuing without tools")
                    yield AgentEvent("warning", {"message": f"Tools unavailable ({type(exc).__name__}); answering without them."})
                    session, tool_specs = None, []
            if not tool_specs:
                messages = strip_tool_blocks(messages)

            while True:
                if iterations >= self.settings.max_iterations:
                    yield AgentEvent("warning", {"message": f"Stopped after {iterations} model calls (iteration cap)."})
                    stop_reason = "iteration_cap"
                    break
                if self.clock() - started > self.settings.request_deadline_s:
                    yield AgentEvent("warning", {"message": "Stopped: request deadline reached."})
                    stop_reason = "deadline"
                    break
                iterations += 1

                request = self._request(spec, messages, tool_specs)
                t0 = time.perf_counter()
                stream = await asyncio.to_thread(self._open_stream, request)

                blocks: dict[int, dict] = {}
                async for event in self._aiter(stream):
                    if "contentBlockStart" in event:
                        start = event["contentBlockStart"]
                        tu = (start.get("start") or {}).get("toolUse")
                        if tu:
                            blocks[start["contentBlockIndex"]] = {"toolUse": {"toolUseId": tu["toolUseId"], "name": tu["name"], "_raw": ""}}
                    elif "contentBlockDelta" in event:
                        d = event["contentBlockDelta"]
                        idx, delta = d["contentBlockIndex"], d["delta"]
                        if "text" in delta:
                            blocks.setdefault(idx, {"text": ""})
                            blocks[idx]["text"] += delta["text"]
                            yield AgentEvent("text", {"delta": delta["text"]})
                        elif "toolUse" in delta:
                            blocks.setdefault(idx, {"toolUse": {"toolUseId": "", "name": "", "_raw": ""}})
                            blocks[idx]["toolUse"]["_raw"] += delta["toolUse"].get("input", "")
                    elif "messageStop" in event:
                        stop_reason = event["messageStop"].get("stopReason")
                    elif "metadata" in event:
                        u = event["metadata"].get("usage") or {}
                        usage["inputTokens"] += int(u.get("inputTokens", 0))
                        usage["outputTokens"] += int(u.get("outputTokens", 0))

                self.on_metric("ModelLatencyMs", (time.perf_counter() - t0) * 1000, {"model": spec.id})

                content: list[dict] = []
                tool_uses: list[dict] = []
                for idx in sorted(blocks):
                    b = blocks[idx]
                    if "text" in b:
                        if b["text"]:
                            content.append({"text": b["text"]})
                    else:
                        raw = b["toolUse"].pop("_raw")
                        try:
                            b["toolUse"]["input"] = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            b["toolUse"]["input"] = {"_invalid_json": raw[:500]}
                        content.append(b)
                        tool_uses.append(b["toolUse"])
                if content:
                    messages.append({"role": "assistant", "content": content})

                if stop_reason != "tool_use" or not tool_uses:
                    break

                if session is None:
                    # The model asked for a tool we cannot run; tell it, and stop.
                    yield AgentEvent("warning", {"message": "Model requested a tool but tools are unavailable."})
                    break

                for tu in tool_uses:
                    yield AgentEvent("tool_use", {"id": tu["toolUseId"], "name": tu["name"], "input": tu["input"]})
                results = await asyncio.gather(*(self._run_tool(session, tu) for tu in tool_uses))
                tool_calls += len(results)
                result_blocks = []
                for tu, res, ms in results:
                    self.on_metric("ToolLatencyMs", ms, {"tool": tu["name"]})
                    if not res.ok:
                        self.on_metric("ToolErrors", 1, {"tool": tu["name"]})
                    text = res.text
                    if len(text) > self.settings.max_tool_result_chars:
                        text = text[: self.settings.max_tool_result_chars] + " …[truncated]"
                    citations = []
                    if tu["name"] == "search_knowledge_base" and isinstance(res.data, dict):
                        citations = [
                            {"id": r.get("citation_id"), "source": r.get("source"), "score": r.get("score")}
                            for r in res.data.get("results", [])
                        ]
                    yield AgentEvent(
                        "tool_result",
                        {"id": tu["toolUseId"], "name": tu["name"], "ok": res.ok, "ms": round(ms), "citations": citations},
                    )
                    result_blocks.append(
                        {
                            "toolResult": {
                                "toolUseId": tu["toolUseId"],
                                "content": [{"text": text}],
                                "status": "success" if res.ok else "error",
                            }
                        }
                    )
                messages.append({"role": "user", "content": result_blocks})
        finally:
            if entered:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 - closing must not mask the answer
                    log.warning("error closing MCP session", exc_info=True)

        close_dangling_turns(messages)

        self.on_metric("InputTokens", usage["inputTokens"], {"model": spec.id})
        self.on_metric("OutputTokens", usage["outputTokens"], {"model": spec.id})
        self.outcome = AgentOutcome(messages, usage, stop_reason, iterations, tool_calls)
        yield AgentEvent("done", {"usage": usage, "stopReason": stop_reason, "iterations": iterations, "toolCalls": tool_calls})
