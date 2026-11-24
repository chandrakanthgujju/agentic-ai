"""Shared fixtures.

Each Lambda is packaged on its own, so several services have modules with the
same name (``handler.py``). ``load_service`` imports a service's module from its
own directory with a clean module cache, mirroring how Lambda sees it.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "1")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "AgenticAITest")
os.environ.setdefault("POWERTOOLS_DEV", "0")

_SERVICE_MODULES = {"handler", "server", "mock_db", "knowledge_base", "app", "agent_loop", "settings", "schema", "tool_gateway"}

MODELS_JSON = (
    '[{"id":"us.amazon.nova-micro-v1:0","label":"Nova Micro","provider":"Amazon","maxTokens":512},'
    '{"id":"us.anthropic.claude-haiku-4-5-20251001-v1:0","label":"Haiku 4.5","provider":"Anthropic"}]'
)


def load_service(service: str, module: str):
    for name in list(sys.modules):
        if name in _SERVICE_MODULES:
            del sys.modules[name]
    path = str(SERVICES / service)
    sys.path[:] = [p for p in sys.path if not p.startswith(str(SERVICES) + os.sep)]
    sys.path.insert(0, path)
    return importlib.import_module(module)


@pytest.fixture
def models_env(monkeypatch):
    monkeypatch.setenv("MODELS_JSON", MODELS_JSON)
    monkeypatch.setenv("DEFAULT_MODEL_ID", "us.amazon.nova-micro-v1:0")
    from shared.model_catalog import catalog_from_env

    catalog_from_env.cache_clear()
    yield
    catalog_from_env.cache_clear()


class FakeLambdaContext:
    function_name = "test-fn"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:test-fn"
    aws_request_id = "req-123"

    def get_remaining_time_in_millis(self):
        return 30000


@pytest.fixture
def lambda_context():
    return FakeLambdaContext()


def rest_event(
    method: str, path: str, *, claims: dict | None = None, body=None, path_params: dict | None = None, resource: str | None = None
):
    import json

    return {
        "resource": resource or path,
        "path": path,
        "httpMethod": method,
        "headers": {"content-type": "application/json"},
        "multiValueHeaders": {},
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "pathParameters": path_params,
        "stageVariables": None,
        "requestContext": {
            "resourcePath": resource or path,
            "httpMethod": method,
            "path": f"/prod{path}",
            "stage": "prod",
            "requestId": "rid-1",
            "authorizer": {"claims": claims} if claims else None,
        },
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }
