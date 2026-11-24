"""The exact Converse requests the agent sends, checked for all allowlisted models.

Nothing here calls AWS. Requests are validated by botocore against the
bedrock-runtime service model (a Stubber-backed client raises
ParamValidationError for any shape problem before the stubbed error is
returned) and against the stricter rules Amazon Nova documents for tool
schemas and tool names.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from shared.model_catalog import ModelCatalog
from tests.conftest import load_service

ROOT = Path(__file__).resolve().parents[2]
CDK_CONFIG = json.loads((ROOT / "cdk.json").read_text())["context"]["agentic:config"]

mcp_server = load_service("mcp_server", "server")
mock_db = __import__("mock_db")


class _NoKb:
    def retrieve(self, query, top_k):
        return []


def _mcp_tools():
    server = mcp_server.build_server(mock_db.MockCommerceRepository(min_latency_ms=0, max_latency_ms=0), _NoKb())
    return asyncio.run(server.list_tools())


MCP_TOOLS = _mcp_tools()

schema = load_service("agent", "schema")
agent_loop = load_service("agent", "agent_loop")
settings_mod = __import__("settings")

TOOL_SPECS = [schema.to_bedrock_tool_spec(t.name, t.description, t.input_schema) for t in MCP_TOOLS]
CATALOG = ModelCatalog.from_json(json.dumps(CDK_CONFIG["models"]), CDK_CONFIG["defaultModelId"])
MODEL_IDS = [m["id"] for m in CDK_CONFIG["models"]]

# Keys we allow per property. Nova documents only a subset of JSON Schema.
_PROPERTY_KEYS = {"type", "description", "enum", "items", "properties", "required"}

HISTORY = [
    {"role": "user", "content": [{"text": "Where is ORD-5001?"}]},
    {
        "role": "assistant",
        "content": [
            {"text": "Let me check."},
            {"toolUse": {"toolUseId": "tooluse_1", "name": "get_order_status", "input": {"order_id": "ORD-5001"}}},
        ],
    },
    {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": "tooluse_1", "content": [{"text": '{"status":"shipped"}'}], "status": "success"}}],
    },
    {"role": "assistant", "content": [{"text": "It has shipped."}]},
]


def _client():
    return botocore.session.get_session().create_client(
        "bedrock-runtime", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )


def _loop():
    s = settings_mod.AgentSettings(guardrail_id="gr-abc123", guardrail_version="1")
    return agent_loop.AgentLoop(None, None, s)


def _assert_botocore_accepts(request: dict) -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_client_error("converse_stream", service_error_code="StubOk", http_status_code=400)
        with pytest.raises(ClientError) as err:  # ParamValidationError would escape this
            client.converse_stream(**request)
        assert err.value.response["Error"]["Code"] == "StubOk"


def _walk_properties(props: dict):
    for name, prop in props.items():
        yield name, prop
        if isinstance(prop.get("properties"), dict):
            yield from _walk_properties(prop["properties"])
        if isinstance(prop.get("items"), dict):
            yield f"{name}[]", prop["items"]


def test_real_mcp_tools_fit_the_nova_tool_contract():
    assert {t["toolSpec"]["name"] for t in TOOL_SPECS} == {
        "get_customer_info",
        "get_order_status",
        "list_customer_orders",
        "search_products",
        "search_knowledge_base",
    }
    for spec in TOOL_SPECS:
        ts = spec["toolSpec"]
        assert schema.TOOL_NAME_RE.match(ts["name"])
        assert ts["description"].strip()
        top = ts["inputSchema"]["json"]
        assert top["type"] == "object"
        assert set(top) <= {"type", "properties", "required"}, ts["name"]
        for name, prop in _walk_properties(top["properties"]):
            assert set(prop) <= _PROPERTY_KEYS, f"{ts['name']}.{name}: {set(prop) - _PROPERTY_KEYS}"
            assert prop["type"] in {"string", "number", "integer", "boolean", "array", "object"}
        assert set(top.get("required", [])) <= set(top["properties"])


def test_constraints_survive_as_description_hints():
    by_name = {t["toolSpec"]["name"]: t["toolSpec"]["inputSchema"]["json"] for t in TOOL_SPECS}
    cid = by_name["get_customer_info"]["properties"]["customer_id"]
    assert "C001" in cid["description"] and "regex" in cid["description"]
    category = by_name["search_products"]["properties"]["category"]
    assert category == {"type": "string", "enum": ["footwear", "apparel", "gear"]}


@pytest.mark.parametrize("model_id", MODEL_IDS)
def test_first_turn_request_is_valid_for_every_model(model_id):
    spec = CATALOG.resolve(model_id)
    req = _loop()._request(spec, [{"role": "user", "content": [{"text": "hi"}]}], TOOL_SPECS)
    _assert_botocore_accepts(req)
    assert set(req["inferenceConfig"]) == {"maxTokens", "temperature"}  # Claude 4.5 rejects temperature+topP together
    assert 0 <= req["inferenceConfig"]["temperature"] <= 1
    if "amazon.nova" in model_id:
        assert req["inferenceConfig"]["maxTokens"] <= 5000  # Nova v1 documented max output
    assert req["guardrailConfig"] == {"guardrailIdentifier": "gr-abc123", "guardrailVersion": "1", "streamProcessingMode": "async"}
    assert req["messages"][0]["role"] == "user"


@pytest.mark.parametrize("model_id", MODEL_IDS)
def test_follow_up_turn_with_tool_history_is_valid_for_every_model(model_id):
    spec = CATALOG.resolve(model_id)
    messages = HISTORY + [{"role": "user", "content": [{"text": "And ORD-5002?"}]}]
    req = _loop()._request(spec, messages, TOOL_SPECS)
    _assert_botocore_accepts(req)
    roles = [m["role"] for m in req["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))


def test_tool_less_request_is_valid():
    spec = CATALOG.resolve(None)
    from shared.conversations import strip_tool_blocks

    req = _loop()._request(spec, strip_tool_blocks(HISTORY + [{"role": "user", "content": [{"text": "x"}]}]), [])
    assert "toolConfig" not in req
    _assert_botocore_accepts(req)
    assert not any("toolUse" in b or "toolResult" in b for m in req["messages"] for b in m["content"])


def test_normalize_messages_repairs_shapes_converse_rejects():
    broken = [
        {"role": "assistant", "content": [{"text": "orphan"}]},
        {"role": "user", "content": [{"text": "  "}, {"text": "hi"}]},
        {"role": "user", "content": [{"text": "again"}]},
        {"role": "assistant", "content": [{"text": ""}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "get_order_status", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "content": [{"text": ""}], "status": "success"}}]},
    ]
    out = agent_loop.normalize_messages(broken)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == [{"text": "hi"}, {"text": "again"}]
    assert out[2]["content"][0]["toolResult"]["content"] == [{"text": "(empty result)"}]
    _assert_botocore_accepts({"modelId": MODEL_IDS[0], "messages": out})
