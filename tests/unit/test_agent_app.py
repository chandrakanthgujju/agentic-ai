"""HTTP layer of the streaming agent: auth, validation, SSE framing, persistence."""

import json

import pytest
from starlette.testclient import TestClient

from tests.conftest import MODELS_JSON, load_service

app_mod = load_service("agent", "app")
settings_mod = __import__("settings")

from shared.model_catalog import ModelCatalog  # noqa: E402
from tests.unit.test_agent_loop import FakeBedrock, FakeGateway, FakeSession, text_turn, tool_turn  # noqa: E402


class MemoryStore:
    def __init__(self):
        self.items = {}

    def get(self, user, cid):
        item = self.items.get((user, cid))
        return None if item is None else {"messages": item}

    def save(self, user, cid, messages, model_id, title=None):
        self.items[(user, cid)] = messages


def ctx_header(sub="user-1", groups=None):
    claims = {"sub": sub}
    if groups:
        claims["cognito:groups"] = groups
    return {"x-amzn-request-context": json.dumps({"authorizer": {"claims": claims}})}


def parse_sse(text):
    events = []
    for frame in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture
def client_and_deps():
    store = MemoryStore()
    bedrock = FakeBedrock([tool_turn([("t1", "get_order_status", {"order_id": "ORD-5001"})]), text_turn("Shipped.")])
    deps = app_mod.Deps(
        catalog=ModelCatalog.from_json(MODELS_JSON),
        store=store,
        bedrock=bedrock,
        tools=FakeGateway(FakeSession()),
        settings=settings_mod.AgentSettings(),
    )
    app = app_mod.create_app(lambda: deps)
    return TestClient(app), deps


def test_requires_verified_identity(client_and_deps):
    client, _ = client_and_deps
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 401


def test_rejects_model_outside_allowlist(client_and_deps):
    client, _ = client_and_deps
    r = client.post("/api/chat", json={"message": "hi", "modelId": "anthropic.claude-opus-4-5"}, headers=ctx_header())
    assert r.status_code == 400 and "not allowed" in r.json()["message"]


def test_rejects_unknown_fields_and_oversized_messages(client_and_deps):
    client, _ = client_and_deps
    assert client.post("/api/chat", json={"message": "hi", "admin": True}, headers=ctx_header()).status_code == 400
    assert client.post("/api/chat", json={"message": "x" * 4001}, headers=ctx_header()).status_code == 400


def test_streams_sse_and_persists(client_and_deps):
    client, deps = client_and_deps
    r = client.post("/api/chat", json={"message": "Where is ORD-5001?"}, headers=ctx_header())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    types = [t for t, _ in events]
    assert types[0] == "meta" and types[-1] == "done"
    assert "tool_use" in types and "tool_result" in types
    cid = events[0][1]["conversationId"]
    saved = deps.store.items[("user-1", cid)]
    assert saved[0] == {"role": "user", "content": [{"text": "Where is ORD-5001?"}]}
    assert saved[-1]["role"] == "assistant"


def test_unknown_conversation_is_404_for_other_user(client_and_deps):
    client, deps = client_and_deps
    deps.store.items[("owner", "11111111-1111-1111-1111-111111111111")] = []
    r = client.post(
        "/api/chat",
        json={"message": "hi", "conversationId": "11111111-1111-1111-1111-111111111111"},
        headers=ctx_header(sub="intruder"),
    )
    assert r.status_code == 404


def test_model_failure_ends_stream_with_typed_error():
    class Broken:
        def converse_stream(self, **_):
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "enable model access"}}, "ConverseStream")

    deps = app_mod.Deps(ModelCatalog.from_json(MODELS_JSON), MemoryStore(), Broken(), None, settings_mod.AgentSettings())
    client = TestClient(app_mod.create_app(lambda: deps))
    events = parse_sse(client.post("/api/chat", json={"message": "hi"}, headers=ctx_header()).text)
    assert events[-1] == ("error", {"message": "enable model access", "code": "AccessDeniedException"})


def test_public_error_redacts_identifiers_and_hides_internal_errors():
    from botocore.exceptions import ClientError

    exc = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "Guardrail arn:aws:bedrock:us-east-1:123456789012:guardrail/abc not usable by 123456789012 "
                "key AKIAABCDEFGHIJKLMNOP " + "x" * 400,
            }
        },
        "ConverseStream",
    )
    code, msg = app_mod.public_error(exc)
    assert code == "ValidationException"
    assert "123456789012" not in msg and "arn:aws" not in msg and "AKIA" not in msg
    assert msg.startswith("Guardrail <arn> not usable by <account> key <redacted>")
    assert len(msg) <= 300
    assert app_mod.public_error(RuntimeError("secret internals")) == ("RuntimeError", "The model call failed. Please retry.")


def test_failed_first_turn_is_not_persisted():
    class Broken:
        def converse_stream(self, **_):
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "ConverseStream")

    store = MemoryStore()
    deps = app_mod.Deps(ModelCatalog.from_json(MODELS_JSON), store, Broken(), None, settings_mod.AgentSettings())
    client = TestClient(app_mod.create_app(lambda: deps))
    events = parse_sse(client.post("/api/chat", json={"message": "hi"}, headers=ctx_header()).text)
    assert events[-1] == ("error", {"message": "bad", "code": "ValidationException"})
    assert not any(e[0] == "text" for e in events)
    assert store.items == {}


def test_healthz():
    client = TestClient(app_mod.create_app(lambda: None))
    assert client.get("/healthz").json() == {"ok": True}
