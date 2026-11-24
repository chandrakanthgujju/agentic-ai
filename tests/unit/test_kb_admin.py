import json
from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from tests.conftest import load_service, rest_event

ADMIN = {"sub": "admin-1", "cognito:groups": "admin"}
USER = {"sub": "user-1"}


@pytest.fixture
def kb(monkeypatch):
    monkeypatch.setenv("DOCS_BUCKET", "docs-bucket")
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "KB1")
    monkeypatch.setenv("DATA_SOURCE_ID", "DS1")
    with mock_aws():
        mod = load_service("kb_admin", "handler")
        mod._clients.clear()
        mod.s3().create_bucket(Bucket="docs-bucket")
        yield mod


def call(mod, ctx, method, path, claims=ADMIN, **kw):
    resp = mod.handler(rest_event(method, path, claims=claims, **kw), ctx)
    return resp["statusCode"], json.loads(resp["body"]) if resp.get("body") else None


def test_non_admin_is_forbidden(kb, lambda_context):
    for method, path in (("GET", "/api/kb/documents"), ("POST", "/api/kb/sync"), ("GET", "/api/kb/sync")):
        assert call(kb, lambda_context, method, path, claims=USER)[0] == 403


def test_presigned_post_pins_key_type_and_size(kb, lambda_context):
    status, body = call(kb, lambda_context, "POST", "/api/kb/documents", body={"filename": "Return Policy.md", "size": 1234})
    assert status == 201
    assert body["key"] == "docs/Return Policy.md"
    assert body["upload"]["url"] == "https://docs-bucket.s3.us-east-1.amazonaws.com/"
    fields = body["upload"]["fields"]
    assert fields["key"] == "docs/Return Policy.md" and fields["Content-Type"] == "text/markdown"
    policy = json.loads(__import__("base64").b64decode(fields["policy"]))
    assert ["content-length-range", 1, 50 * 1024 * 1024] in policy["conditions"]


@pytest.mark.parametrize("name", ["../etc/passwd.md", "evil.exe", ".hidden.md", "a" * 130 + ".md"])
def test_rejects_bad_filenames(kb, lambda_context, name):
    status, _ = call(kb, lambda_context, "POST", "/api/kb/documents", body={"filename": name, "size": 10})
    assert status == 400


def test_rejects_oversized_upload(kb, lambda_context):
    status, _ = call(kb, lambda_context, "POST", "/api/kb/documents", body={"filename": "big.pdf", "size": 60 * 1024 * 1024})
    assert status == 400


def test_list_and_delete(kb, lambda_context):
    kb.s3().put_object(Bucket="docs-bucket", Key="docs/faq.md", Body=b"# FAQ")
    kb.s3().put_object(Bucket="docs-bucket", Key="other/secret.txt", Body=b"x")
    status, body = call(kb, lambda_context, "GET", "/api/kb/documents")
    assert status == 200 and [d["name"] for d in body["documents"]] == ["faq.md"]

    status, _ = call(
        kb, lambda_context, "DELETE", "/api/kb/documents/faq.md", path_params={"name": "faq.md"}, resource="/api/kb/documents/{name}"
    )
    assert status == 200
    status, _ = call(
        kb, lambda_context, "DELETE", "/api/kb/documents/faq.md", path_params={"name": "faq.md"}, resource="/api/kb/documents/{name}"
    )
    assert status == 404


def test_sync_start_conflict_and_status(kb, lambda_context):
    client = boto3.client("bedrock-agent", region_name="us-east-1")
    kb._clients["agent"] = client
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    with Stubber(client) as stub:
        stub.add_response(
            "start_ingestion_job",
            {
                "ingestionJob": {
                    "knowledgeBaseId": "KB1",
                    "dataSourceId": "DS1",
                    "ingestionJobId": "JOB1",
                    "status": "STARTING",
                    "startedAt": now,
                    "updatedAt": now,
                }
            },
            {"knowledgeBaseId": "KB1", "dataSourceId": "DS1", "description": "manual sync from admin UI"},
        )
        stub.add_client_error("start_ingestion_job", service_error_code="ConflictException", http_status_code=409)
        stub.add_response(
            "list_ingestion_jobs",
            {
                "ingestionJobSummaries": [
                    {
                        "knowledgeBaseId": "KB1",
                        "dataSourceId": "DS1",
                        "ingestionJobId": "JOB1",
                        "status": "COMPLETE",
                        "startedAt": now,
                        "updatedAt": now,
                        "statistics": {"numberOfDocumentsScanned": 3},
                    }
                ]
            },
        )
        assert call(kb, lambda_context, "POST", "/api/kb/sync") == (202, {"jobId": "JOB1", "status": "STARTING"})
        assert call(kb, lambda_context, "POST", "/api/kb/sync")[0] == 409
        status, body = call(kb, lambda_context, "GET", "/api/kb/sync")
    assert status == 200 and body["jobs"][0]["statistics"]["numberOfDocumentsScanned"] == 3
