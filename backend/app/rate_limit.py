"""Small in-process rate limiter for public authentication and billing routes.

It intentionally has no external dependency.  Deployments with multiple app
instances should replace it with a shared (for example Redis) limiter.
"""
from __future__ import annotations

from collections import defaultdict, deque
from time import monotonic

from fastapi import HTTPException, Request
from app.settings import get_settings

_requests: dict[str, deque[float]] = defaultdict(deque)
_MAX_BUCKETS = 10_000


def _client_key(request: Request) -> str:
    """Use forwarded client IP only when an operator explicitly trusts proxy headers."""
    settings = get_settings()
    if settings.trusted_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def _prune_buckets(now: float) -> None:
    if len(_requests) <= _MAX_BUCKETS:
        return
    # A bounded, opportunistic cleanup avoids attacker-driven IP churn growing
    # process memory indefinitely without adding a timer/task to the app.
    for key, entries in list(_requests.items()):
        if not entries or entries[-1] <= now - 24 * 60 * 60:
            _requests.pop(key, None)
        if len(_requests) <= _MAX_BUCKETS:
            break
    # An attacker can keep every churned bucket newer than 24 hours. Enforce
    # the memory ceiling anyway by evicting the least-recently-used buckets.
    if len(_requests) > _MAX_BUCKETS:
        oldest = sorted(
            _requests,
            key=lambda key: _requests[key][-1] if _requests[key] else float("-inf"),
        )
        for key in oldest[: len(_requests) - _MAX_BUCKETS]:
            _requests.pop(key, None)


def enforce_rate_limit(
    request: Request,
    bucket: str,
    limit: int,
    window_seconds: int,
    *,
    identity: str | None = None,
) -> None:
    client = identity or _client_key(request)
    key = f"{bucket}:{client}"
    now = monotonic()
    _prune_buckets(now)
    entries = _requests[key]
    cutoff = now - window_seconds
    while entries and entries[0] <= cutoff:
        entries.popleft()
    if len(entries) >= limit:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a few minutes and try again.",
            headers={"Retry-After": str(window_seconds)},
        )
    entries.append(now)
