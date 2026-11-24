import json

import boto3
from botocore.stub import ANY, Stubber

from tests.conftest import load_service


def _records(n):
    return [{"messageId": f"m{i}", "body": json.dumps({"detail": {"object": {"key": f"docs/f{i}.md"}}})} for i in range(n)]


def _setup(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "KB1")
    monkeypatch.setenv("DATA_SOURCE_ID", "DS1")
    mod = load_service("ingestion", "handler")
    mod._client = boto3.client("bedrock-agent", region_name="us-east-1")
    return mod


def test_one_job_per_batch_with_idempotency_token(monkeypatch, lambda_context):
    mod = _setup(monkeypatch)
    recs = _records(3)
    with Stubber(mod._client) as stub:
        stub.add_response(
            "start_ingestion_job",
            {
                "ingestionJob": {
                    "knowledgeBaseId": "KB1",
                    "dataSourceId": "DS1",
                    "ingestionJobId": "J1",
                    "status": "STARTING",
                    "startedAt": "2026-11-24T00:00:00Z",
                    "updatedAt": "2026-11-24T00:00:00Z",
                }
            },
            {"knowledgeBaseId": "KB1", "dataSourceId": "DS1", "clientToken": mod.client_token(recs), "description": ANY},
        )
        assert mod.handler({"Records": recs}, lambda_context) == {"batchItemFailures": []}
    assert mod.client_token(recs) == mod.client_token(list(reversed(recs)))  # order-independent


def test_running_job_defers_whole_batch(monkeypatch, lambda_context):
    mod = _setup(monkeypatch)
    with Stubber(mod._client) as stub:
        stub.add_client_error("start_ingestion_job", service_error_code="ConflictException", http_status_code=409)
        out = mod.handler({"Records": _records(2)}, lambda_context)
    assert out == {"batchItemFailures": [{"itemIdentifier": "m0"}, {"itemIdentifier": "m1"}]}


def test_empty_batch(monkeypatch, lambda_context):
    assert _setup(monkeypatch).handler({"Records": []}, lambda_context) == {"batchItemFailures": []}
