"""The MCP server behind a Lambda Function URL: several invocations on one warm process."""

import json

from tests.conftest import load_service


def _event(body: dict) -> dict:
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/mcp",
        "rawQueryString": "",
        "headers": {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "host": "abc.lambda-url.us-east-1.on.aws",
        },
        "requestContext": {
            "http": {"method": "POST", "path": "/mcp", "protocol": "HTTP/1.1", "sourceIp": "10.0.0.1", "userAgent": "test"},
            "domainName": "abc.lambda-url.us-east-1.on.aws",
            "requestId": "r1",
            "stage": "$default",
            "timeEpoch": 0,
        },
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def test_function_url_round_trips(monkeypatch, lambda_context):
    monkeypatch.setenv("MOCK_DB_MAX_LATENCY_MS", "0")
    monkeypatch.setenv("MOCK_DB_MIN_LATENCY_MS", "0")
    handler = load_service("mcp_server", "handler")

    listed = handler.handler(_event({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}), lambda_context)
    assert listed["statusCode"] == 200
    names = {t["name"] for t in json.loads(listed["body"])["result"]["tools"]}
    assert "get_customer_info" in names

    for i in range(2):  # the session manager must survive across invocations
        called = handler.handler(
            _event(
                {
                    "jsonrpc": "2.0",
                    "id": 10 + i,
                    "method": "tools/call",
                    "params": {"name": "get_order_status", "arguments": {"order_id": "ORD-5003"}},
                }
            ),
            lambda_context,
        )
        body = json.loads(called["body"])
        assert called["statusCode"] == 200
        assert body["result"]["isError"] is False
        assert "processing" in json.dumps(body["result"])
