"""Small in-process rate limiter for public authentication and billing routes.

It intentionally has no external dependency.  Deployments with multiple app
instances should replace it with a shared (for example Redis) limiter.
"""
from __future__ import annotations

from collections import defaultdict, deque
from time import monotonic

from fastapi import HTTPException, Request

_requests: dict[str, deque[float]] = defaultdict(deque)


def enforce_rate_limit(request: Request, bucket: str, limit: int, window_seconds: int) -> None:
    client = request.client.host if request.client else "unknown"
    key = f"{bucket}:{client}"
    now = monotonic()
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
