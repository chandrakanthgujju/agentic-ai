"""Exponential backoff with full jitter for retryable AWS errors.

botocore already retries (we configure ``adaptive`` mode), but Bedrock
throttling on a cold, low-quota account is common enough that the agent adds
one more bounded layer around *starting* a model stream. Retrying after tokens
have been streamed to the user would duplicate output, so callers only wrap the
call that opens the stream.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from botocore.exceptions import ClientError

RETRYABLE_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "ModelNotReadyException",
        "InternalServerException",
    }
)


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in RETRYABLE_CODES


def backoff_delays(attempts: int, base: float = 0.5, cap: float = 8.0, rng: random.Random | None = None):
    """Full-jitter delays: sleep = uniform(0, min(cap, base * 2**n))."""
    r = rng or random
    for n in range(attempts):
        yield r.uniform(0, min(cap, base * (2**n)))


def call_with_backoff[T](
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base: float = 0.5,
    cap: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    delays = list(backoff_delays(attempts - 1, base, cap, rng))
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - filtered by is_retryable
            if attempt == attempts - 1 or not is_retryable(exc):
                raise
            delay = delays[attempt]
            if on_retry:
                on_retry(attempt + 1, exc, delay)
            sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover
