import random

import pytest
from botocore.exceptions import ClientError

from shared.retry import backoff_delays, call_with_backoff
from tests.conftest import load_service

schema = load_service("agent", "schema")


def test_input_schema_collapses_optional_and_moves_constraints_into_descriptions():
    src = {
        "title": "Args",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"anyOf": [{"type": "string", "maxLength": 100}, {"type": "null"}], "default": None, "title": "Query"},
            "limit": {"type": "integer", "minimum": 1, "title": "Limit", "description": "Max rows"},
        },
        "required": ["limit"],
    }
    out = schema.to_input_schema(src)
    assert out == {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "(at most 100 characters)"},
            "limit": {"type": "integer", "description": "Max rows (minimum 1)"},
        },
        "required": ["limit"],
    }


def test_tool_spec_rejects_names_nova_does_not_accept():
    for bad in ("bad name!", "read-query", "1tool", "", "x" * 65):
        with pytest.raises(schema.InvalidToolError):
            schema.to_bedrock_tool_spec(bad, "d", {})
    spec = schema.to_bedrock_tool_spec("ok_name", None, {})
    assert spec["toolSpec"]["inputSchema"]["json"] == {"type": "object", "properties": {}}


def test_ref_schemas_are_rejected_at_discovery_time():
    with pytest.raises(schema.InvalidToolError):
        schema.to_input_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"type": "object"}}})


def test_full_jitter_delays_are_bounded():
    delays = list(backoff_delays(6, base=0.5, cap=4.0, rng=random.Random(1)))
    assert len(delays) == 6
    assert all(0 <= d <= min(4.0, 0.5 * 2**i) for i, d in enumerate(delays))


def test_call_with_backoff_retries_only_retryable():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Op")
        return "ok"

    assert call_with_backoff(flaky, sleep=lambda _: None) == "ok"
    assert calls["n"] == 3

    def denied():
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "Op")

    with pytest.raises(ClientError):
        call_with_backoff(denied, sleep=lambda _: None)


def test_call_with_backoff_gives_up():
    def always():
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Op")

    with pytest.raises(ClientError):
        call_with_backoff(always, attempts=3, sleep=lambda _: None)
