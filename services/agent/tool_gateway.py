"""MCP client side: discover tools, map them to Bedrock, and call them.

The agent never hard-codes tool schemas. It lists tools from the MCP server
(cached for ``cache_ttl_s`` per warm Lambda), so adding or versioning a tool is a
deploy of the tool server only. One MCP client session is opened per chat
request and reused for every tool call in that request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol
from collections.abc import AsyncIterator

from schema import InvalidToolError, to_bedrock_tool_spec

log = logging.getLogger(__name__)


@dataclass
class ToolResult:
    ok: bool
    text: str
    data: Any = None


class ToolSession(Protocol):
    async def list_tool_specs(self) -> list[dict]: ...
    async def call_tool(self, name: str, arguments: dict) -> ToolResult: ...


class ToolGateway(Protocol):
    def open(self) -> contextlib.AbstractAsyncContextManager[ToolSession]: ...


class _McpSession:
    def __init__(self, gateway: "McpToolGateway", client):
        self._gw = gateway
        self._client = client

    async def list_tool_specs(self) -> list[dict]:
        cache = self._gw._cache
        if cache and time.monotonic() - cache[0] < self._gw.cache_ttl_s:
            return cache[1]
        listed = await asyncio.wait_for(self._client.list_tools(), self._gw.timeout_s)
        specs, names = [], set()
        for t in listed.tools:
            try:
                specs.append(to_bedrock_tool_spec(t.name, t.description, t.input_schema))
                names.add(t.name)
            except InvalidToolError as exc:
                # One bad tool must not take the whole catalog (or the chat) down.
                log.error("skipping MCP tool that Bedrock would reject", extra={"tool": t.name, "error": str(exc)})
        self._gw._cache = (time.monotonic(), specs, names)
        return specs

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        cache = self._gw._cache
        if cache and name not in cache[2]:
            return ToolResult(False, f"unknown tool '{name}'")
        result = await asyncio.wait_for(self._client.call_tool(name, arguments), self._gw.timeout_s)
        texts = [getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text"]
        data = result.structured_content
        text = json.dumps(data) if data is not None else "\n".join(texts)
        return ToolResult(not result.is_error, text, data)


class McpToolGateway:
    """Talks MCP Streamable HTTP to the tool server, signing requests with SigV4."""

    def __init__(self, url: str, region: str, cache_ttl_s: float = 300.0, timeout_s: float = 15.0, credentials_provider=None):
        self.url = url
        self.region = region
        self.cache_ttl_s = cache_ttl_s
        self.timeout_s = timeout_s
        self._credentials_provider = credentials_provider
        self._cache: tuple[float, list[dict], set[str]] | None = None

    def _make_client(self):
        # Imported lazily so unit tests that inject a fake gateway skip the SDK.
        import boto3
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        from shared.sigv4 import SigV4HttpxAuth

        provider = self._credentials_provider or boto3.Session().get_credentials
        http = create_mcp_http_client(auth=SigV4HttpxAuth(provider, self.region, "lambda"))
        transport = streamable_http_client(self.url, http_client=http, terminate_on_close=False)
        return http, Client(transport, read_timeout_seconds=self.timeout_s)

    @contextlib.asynccontextmanager
    async def open(self) -> AsyncIterator[ToolSession]:
        http, client = self._make_client()
        async with http, client:
            yield _McpSession(self, client)
