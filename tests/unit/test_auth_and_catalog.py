import json

import pytest

from shared.auth import UnauthorizedError, caller_from_lwa_header, caller_from_rest_event
from shared.model_catalog import ModelCatalog, ModelSpec


@pytest.mark.parametrize(
    "raw,expected",
    [("admin", {"admin"}), ("[admin, analysts]", {"admin", "analysts"}), (["admin"], {"admin"}), (None, set()), ("a,b", {"a", "b"})],
)
def test_group_claim_formats(raw, expected):
    claims = {"sub": "u1", "cognito:groups": raw} if raw is not None else {"sub": "u1"}
    caller = caller_from_rest_event({"requestContext": {"authorizer": {"claims": claims}}})
    assert caller.groups == frozenset(expected)
    assert caller.is_admin == ("admin" in expected)


def test_missing_claims_rejected():
    with pytest.raises(UnauthorizedError):
        caller_from_rest_event({"requestContext": {}})
    with pytest.raises(UnauthorizedError):
        caller_from_lwa_header(None)
    with pytest.raises(UnauthorizedError):
        caller_from_lwa_header("{not json")


def test_lwa_request_context_header():
    header = json.dumps({"authorizer": {"claims": {"sub": "abc", "email": "a@example.com"}}})
    caller = caller_from_lwa_header(header)
    assert caller.user_id == "abc" and caller.email == "a@example.com" and not caller.is_admin


def test_model_allowlist():
    cat = ModelCatalog([ModelSpec(id="m1", label="M1", provider="p"), ModelSpec(id="m2", label="M2", provider="p")], default_id="m2")
    assert cat.resolve(None).id == "m2"
    assert cat.resolve("m1").id == "m1"
    with pytest.raises(ValueError):
        cat.resolve("anthropic.claude-opus-4-5")  # not allowlisted, even if valid in Bedrock
    assert [m["default"] for m in cat.public_list()] == [False, True]
