import json

import boto3
import pytest
from moto import mock_aws

from tests.conftest import load_service, rest_event

CID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def api(monkeypatch, models_env):
    monkeypatch.setenv("CONVERSATIONS_TABLE", "conv")
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="conv",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        mod = load_service("api", "handler")
        mod._store = None
        yield mod


def call(mod, ctx, method, path, claims={"sub": "alice"}, **kw):
    resp = mod.handler(rest_event(method, path, claims=claims, **kw), ctx)
    return resp["statusCode"], json.loads(resp["body"]) if resp.get("body") else None


def test_models_endpoint_lists_allowlist(api, lambda_context):
    status, body = call(api, lambda_context, "GET", "/api/models")
    assert status == 200
    assert [m["id"] for m in body["models"]] == ["us.amazon.nova-micro-v1:0", "us.anthropic.claude-haiku-4-5-20251001-v1:0"]


def test_unauthenticated_is_401(api, lambda_context):
    status, _ = call(api, lambda_context, "GET", "/api/conversations", claims=None)
    assert status == 401


def test_conversation_crud_is_scoped_to_caller(api, lambda_context):
    api.store().save(
        "alice", CID, [{"role": "user", "content": [{"text": "hello"}]}, {"role": "assistant", "content": [{"text": "hi"}]}], "m", "hello"
    )
    status, body = call(api, lambda_context, "GET", "/api/conversations")
    assert status == 200 and body["conversations"][0]["conversationId"] == CID

    status, body = call(api, lambda_context, "GET", f"/api/conversations/{CID}", path_params={"conversationId": CID})
    assert status == 200 and body["messages"][1]["text"] == "hi"

    status, _ = call(
        api, lambda_context, "GET", f"/api/conversations/{CID}", claims={"sub": "mallory"}, path_params={"conversationId": CID}
    )
    assert status == 404

    status, _ = call(api, lambda_context, "DELETE", f"/api/conversations/{CID}", path_params={"conversationId": CID})
    assert status == 200
    assert api.store().get("alice", CID) is None


def test_invalid_conversation_id(api, lambda_context):
    status, _ = call(api, lambda_context, "GET", "/api/conversations/not-a-uuid", path_params={"conversationId": "not-a-uuid"})
    assert status == 400
