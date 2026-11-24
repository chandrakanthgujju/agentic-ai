"""Caller identity extracted from the API Gateway Cognito authorizer.

API Gateway has already verified the JWT (signature, expiry, audience) before a
request reaches any Lambda, so handlers only *read* the verified claims here.
They never trust identity sent in the request body.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Mapping

ADMIN_GROUP = "admin"


class UnauthorizedError(Exception):
    """The request carried no verified identity."""


@dataclass(frozen=True)
class Caller:
    user_id: str
    email: str | None = None
    groups: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_admin(self) -> bool:
        return ADMIN_GROUP in self.groups


def _parse_groups(raw: Any) -> frozenset[str]:
    """``cognito:groups`` arrives as a list, a JSON-ish "[a, b]" string, or "a,b"."""
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set)):
        return frozenset(str(g) for g in raw if g)
    text = str(raw).strip().strip("[]")
    return frozenset(g for g in re.split(r"[\s,]+", text) if g)


def caller_from_claims(claims: Mapping[str, Any] | None) -> Caller:
    if not claims or not claims.get("sub"):
        raise UnauthorizedError("missing verified claims")
    return Caller(
        user_id=str(claims["sub"]),
        email=claims.get("email"),
        groups=_parse_groups(claims.get("cognito:groups")),
    )


def caller_from_rest_event(event: Mapping[str, Any]) -> Caller:
    """REST API (v1 payload) Lambda proxy event -> Caller."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims")
    return caller_from_claims(claims)


def caller_from_lwa_header(header_value: str | None) -> Caller:
    """Lambda Web Adapter forwards the API Gateway requestContext as JSON in the
    ``x-amzn-request-context`` header. Only API Gateway can invoke the function
    (resource policy), and the adapter overwrites any client-sent value."""
    if not header_value:
        raise UnauthorizedError("missing request context")
    try:
        ctx = json.loads(header_value)
    except json.JSONDecodeError as exc:
        raise UnauthorizedError("malformed request context") from exc
    claims = (ctx.get("authorizer") or {}).get("claims")
    return caller_from_claims(claims)
