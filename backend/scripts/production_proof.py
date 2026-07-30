"""Repeatable live production proof for the public Radar and market-data plane.

This script intentionally avoids AI calls, payment mutations, account creation,
and trade publication. It proves that the deployed application is reachable,
that every shared Radar pair serves one synchronized server snapshot, that at
least one configured symbol receives qualified public execution flow, and that
premium single-symbol research remains protected.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


RADAR_PAIRS = (("5m", "1h"), ("15m", "4h"), ("1h", "1d"))


@dataclass
class Proof:
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def record(self, name: str, passed: bool, **evidence: Any) -> None:
        self.checks.append({"name": name, "passed": passed, **evidence})
        if not passed:
            self.failures.append(name)


def _request(
    base_url: str,
    path: str,
    *,
    timeout: float,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any, dict[str, str], str]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            content_type = response.headers.get("Content-Type", "")
            data = json.loads(raw) if "json" in content_type else raw
            return response.status, data, dict(response.headers.items()), response.geturl()
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw
        return exc.code, data, dict(exc.headers.items()), exc.geturl()
    except (TimeoutError, URLError) as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc


def _header(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    return next((value for key, value in headers.items() if key.lower() == target), None)


def run_proof(args: argparse.Namespace) -> Proof:
    proof = Proof()

    status, health, _, _ = _request(args.base_url, "/health", timeout=args.timeout)
    proof.record(
        "application_health",
        status == 200 and isinstance(health, dict) and health.get("status") == "ok",
        http_status=status,
        mode=health.get("mode") if isinstance(health, dict) else None,
        version=health.get("version") if isinstance(health, dict) else None,
    )

    status, page, _, final_url = _request(args.base_url, "/", timeout=args.timeout)
    proof.record(
        "radar_is_primary_page",
        status == 200 and final_url.endswith("/static/radar.html") and "Causal Flight Deck" in str(page),
        http_status=status,
        final_url=final_url,
    )

    radar_evidence: dict[str, Any] = {}
    for lower, higher in RADAR_PAIRS:
        query = urlencode({"ltf": lower, "htf": higher})
        status, rows, headers, _ = _request(
            args.base_url,
            f"/quant/breakout-radar?{query}",
            timeout=args.timeout,
        )
        snapshot_at = _header(headers, "X-Radar-Snapshot-At")
        next_refresh_at = _header(headers, "X-Radar-Next-Refresh-At")
        state = _header(headers, "X-Radar-Snapshot-State")
        key = f"{lower}/{higher}"
        radar_evidence[key] = {
            "http_status": status,
            "candidates": len(rows) if isinstance(rows, list) else 0,
            "state": state,
            "snapshot_at": snapshot_at,
            "next_refresh_at": next_refresh_at,
        }
        proof.record(
            f"radar_pair_{key}",
            status == 200
            and isinstance(rows, list)
            and bool(rows)
            and bool(snapshot_at)
            and bool(next_refresh_at)
            and state in {"FRESH", "STALE_REFRESHING"},
            **radar_evidence[key],
        )

    sync_query = urlencode({"ltf": "15m", "htf": "4h"})

    def fetch_sync_sample() -> tuple[int, Any, dict[str, str], str]:
        return _request(
            args.base_url,
            f"/quant/breakout-radar?{sync_query}",
            timeout=args.timeout,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(fetch_sync_sample)
        second_future = executor.submit(fetch_sync_sample)
        first = first_future.result()
        second = second_future.result()
    first_snapshot = _header(first[2], "X-Radar-Snapshot-At")
    second_snapshot = _header(second[2], "X-Radar-Snapshot-At")
    first_refresh = _header(first[2], "X-Radar-Next-Refresh-At")
    second_refresh = _header(second[2], "X-Radar-Next-Refresh-At")
    proof.record(
        "shared_radar_clock",
        first[0] == 200
        and second[0] == 200
        and bool(first_snapshot)
        and first_snapshot == second_snapshot
        and bool(first_refresh)
        and first_refresh == second_refresh,
        first_snapshot=first_snapshot,
        second_snapshot=second_snapshot,
        first_next_refresh=first_refresh,
        second_next_refresh=second_refresh,
    )

    unsupported_status, _, _, _ = _request(
        args.base_url,
        "/quant/breakout-radar?ltf=3m&htf=2h",
        timeout=args.timeout,
    )
    proof.record(
        "unsupported_radar_pair_rejected",
        unsupported_status == 422,
        http_status=unsupported_status,
    )

    research_status, _, _, _ = _request(
        args.base_url,
        "/quant/verify-setup",
        timeout=args.timeout,
        method="POST",
        payload={"symbol": "BTCUSDT", "ltf": "15m", "htf": "4h"},
    )
    proof.record(
        "premium_research_is_protected",
        research_status in {401, 403},
        http_status=research_status,
    )

    deadline = time.monotonic() + max(0.0, args.duration)
    samples: list[dict[str, Any]] = []
    max_qualified_symbols = 0
    final_qualified_symbols = 0
    while True:
        sample_started = time.monotonic()
        status, market_health, _, _ = _request(
            args.base_url,
            "/health/market-data",
            timeout=args.timeout,
        )
        symbols = market_health.get("symbols", {}) if isinstance(market_health, dict) else {}
        qualified = {
            symbol: int(payload.get("qualified_source_count") or 0)
            for symbol, payload in symbols.items()
            if int(payload.get("qualified_source_count") or 0) > 0
        }
        max_qualified_symbols = max(max_qualified_symbols, len(qualified))
        final_qualified_symbols = len(qualified)
        samples.append(
            {
                "http_status": status,
                "status": market_health.get("status") if isinstance(market_health, dict) else None,
                "qualified_symbols": qualified,
                "subscription_errors": (
                    (market_health.get("metrics") or {}).get("subscription_errors")
                    if isinstance(market_health, dict)
                    else None
                ),
                "stale_events_dropped": (
                    (market_health.get("metrics") or {}).get("stale_events_dropped")
                    if isinstance(market_health, dict)
                    else None
                ),
            }
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.5, args.interval), remaining))
        if time.monotonic() - sample_started > args.timeout + args.interval + 5:
            break

    proof.record(
        "qualified_public_execution_flow",
        max_qualified_symbols >= args.min_qualified_symbols
        and final_qualified_symbols >= args.min_qualified_symbols,
        duration_seconds=args.duration,
        samples=len(samples),
        max_qualified_symbols=max_qualified_symbols,
        final_qualified_symbols=final_qualified_symbols,
        final_sample=samples[-1] if samples else None,
        note="Individual unsupported venues or pairs may remain partial/unavailable.",
    )
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--min-qualified-symbols", type=int, default=1)
    args = parser.parse_args()

    try:
        proof = run_proof(args)
    except Exception as exc:
        print(json.dumps({"passed": False, "fatal_error": str(exc)}, indent=2))
        return 1

    print(
        json.dumps(
            {
                "passed": not proof.failures,
                "base_url": args.base_url,
                "failures": proof.failures,
                "checks": proof.checks,
            },
            indent=2,
        )
    )
    return 0 if not proof.failures else 1


if __name__ == "__main__":
    sys.exit(main())
