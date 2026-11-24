"""MCP tool server: discovery, schemas, versions, validation and results (in-process MCP client)."""

import importlib
import json

import pytest
from mcp import Client

from tests.conftest import load_service


class FakeRetriever:
    def __init__(self):
        self.calls = []

    def retrieve(self, query, top_k):
        self.calls.append((query, top_k))
        return [
            {"citation_id": 1, "source": "returns.md", "uri": "s3://b/docs/returns.md", "score": 0.81, "text": "Returns within 30 days."}
        ]


@pytest.fixture
def server_parts():
    server_mod = load_service("mcp_server", "server")
    mock_db = importlib.import_module("mock_db")  # same module object the server imported
    repo = mock_db.MockCommerceRepository(min_latency_ms=0, max_latency_ms=0)
    retriever = FakeRetriever()
    return server_mod.build_server(repo, retriever), repo, retriever


def _payload(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_lists_versioned_read_only_tools(server_parts):
    server, _, _ = server_parts
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_customer_info", "get_order_status", "list_customer_orders", "search_products", "search_knowledge_base"}
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.meta["version"]
    schema = tools["get_order_status"].input_schema
    assert schema["required"] == ["order_id"]
    assert schema["properties"]["order_id"]["pattern"] == r"^ORD-\d{4}$"


@pytest.mark.anyio
async def test_get_customer_info_simulates_query(server_parts):
    server, repo, _ = server_parts
    async with Client(server) as client:
        result = await client.call_tool("get_customer_info", {"customer_id": "C001"})
    data = _payload(result)
    assert result.is_error is False
    assert data["found"] is True and data["customer"]["name"] == "Asha Rao"
    assert data["recent_orders"][0]["order_id"] == "ORD-5001"
    assert any("FROM customers" in sql for sql, _ in repo.queries)


@pytest.mark.anyio
async def test_not_found_is_a_normal_result(server_parts):
    server, _, _ = server_parts
    async with Client(server) as client:
        data = _payload(await client.call_tool("get_order_status", {"order_id": "ORD-9999"}))
    assert data == {"found": False, "order_id": "ORD-9999"}


@pytest.mark.anyio
async def test_invalid_input_rejected_before_tool_runs(server_parts):
    server, repo, _ = server_parts
    async with Client(server) as client:
        result = await client.call_tool("get_order_status", {"order_id": "DROP TABLE orders"})
    assert result.is_error is True
    assert repo.queries == []


@pytest.mark.anyio
async def test_search_products_filters(server_parts):
    server, _, _ = server_parts
    async with Client(server) as client:
        data = _payload(await client.call_tool("search_products", {"category": "footwear", "in_stock_only": True}))
    assert [p["sku"] for p in data["products"]] == ["SKU-1001"]


@pytest.mark.anyio
async def test_knowledge_base_tool_returns_citations(server_parts):
    server, _, retriever = server_parts
    async with Client(server) as client:
        data = _payload(await client.call_tool("search_knowledge_base", {"query": "return policy", "top_k": 2}))
    assert retriever.calls == [("return policy", 2)]
    assert data["results"][0]["citation_id"] == 1
    assert "not instructions" in data["notice"]


@pytest.fixture
def anyio_backend():
    return "asyncio"
