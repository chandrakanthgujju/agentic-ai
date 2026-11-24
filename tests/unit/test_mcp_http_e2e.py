"""End-to-end over real HTTP: the agent's McpToolGateway (MCP client + SigV4 signing)
talking to the MCP server's Streamable HTTP app served by uvicorn on localhost."""

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from botocore.credentials import Credentials
from mcp.server.transport_security import TransportSecuritySettings

from tests.conftest import load_service


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mcp_url():
    server_mod = load_service("mcp_server", "server")
    mock_db = __import__("mock_db")
    mcp = server_mod.build_server(mock_db.MockCommerceRepository(0, 0))
    inner = mcp.streamable_http_app(
        stateless_http=True, json_response=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    seen = []

    async def capture(scope, receive, send):
        if scope["type"] == "http":
            seen.append(dict(scope["headers"]).get(b"authorization", b"").decode())
        await inner(scope, receive, send)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(capture, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", seen
    server.should_exit = True
    thread.join(timeout=5)


def test_gateway_discovers_and_calls_tools_with_sigv4(mcp_url):
    url, seen = mcp_url
    tool_gateway = load_service("agent", "tool_gateway")
    gw = tool_gateway.McpToolGateway(url, "us-east-1", credentials_provider=lambda: Credentials("AKID", "SECRET"))

    async def go():
        async with gw.open() as session:
            specs = await session.list_tool_specs()
            result = await session.call_tool("get_order_status", {"order_id": "ORD-5005"})
            unknown = await session.call_tool("drop_tables", {})
        return specs, result, unknown

    specs, result, unknown = asyncio.run(go())
    names = {s["toolSpec"]["name"] for s in specs}
    assert {"get_customer_info", "search_knowledge_base"} <= names
    order_schema = next(s for s in specs if s["toolSpec"]["name"] == "get_order_status")["toolSpec"]["inputSchema"]["json"]
    assert "title" not in order_schema
    assert result.ok and "out_for_delivery" in result.text
    assert unknown.ok is False and "unknown tool" in unknown.text
    assert seen and all(h.startswith("AWS4-HMAC-SHA256") for h in seen)
