"""Autonomous Scanner background task — scans the market on a schedule.

Saves high-conviction signals to the database and tracks active scan state.
Supports both a fixed watchlist and dynamic AI pair discovery.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select, text

from app.brains.council import finalize_ai_council_validation, run_ai_council
from app.brains.signal_builder import build_ai_driven_trade_setup, evaluate_ai_driven_approval
from app.data_sources.data_aggregator import (
    attach_live_execution_tape_snapshot,
    fetch_market_intelligence,
    fetch_pair_discovery_data,
)
from app.db.database import AsyncSessionLocal, engine
from app.db.models import ScannerConfiguration, ScannerObservation, TradeSignal
from app.signal_service import _lock_signal_key, get_active_signal, ensure_signal_database
from app.brains.signal_lifecycle import build_signal_seed
from app.quant.live_confirmation import verify_main_signal_snapshot
from app.settings import get_settings

logger = logging.getLogger(__name__)

# ── Global Scanner State ─────────────────────────────────────────────────────

scanner_state: dict[str, Any] = {
    "is_scanning": False,
    "last_scan_time": None,
    "next_scan_time": None,
    "current_symbol": None,
    "scan_history": [],  # List of dicts representing last scan runs
    "watchlist": [],
    "discovered_pairs": [],
    "active_signals_count": 0,
}


def _scanner_observation(
    *, symbol: str, timeframe: str, status: str, decision: str | None = None,
    confidence: float | None = None, approval: dict[str, Any] | None = None,
    live_confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep durable scanner history focused on evidence, never full AI payloads."""
    confirmation = live_confirmation or {}
    scenarios = confirmation.get("scenarios", {}) or {}
    institutional = scenarios.get("institutional", {}) or {}
    tactical = scenarios.get("tactical", {}) or {}
    coverage = confirmation.get("publication_coverage", {}) or {}
    blockers = list((approval or {}).get("blockers", []) or [])
    live_reason = confirmation.get("reason")
    if live_reason and live_reason not in blockers:
        blockers.append(str(live_reason))
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "decision": decision,
        "confidence": confidence,
        "primary_blocker": blockers[0] if blockers else None,
        "blockers": blockers[:8],
        "publication_coverage": coverage,
        "institutional": {
            "passed": bool(institutional.get("passed")),
            "status": institutional.get("status"),
            "playbook": institutional.get("playbook"),
            "reason": institutional.get("reason"),
            "market_story_state": institutional.get("market_story_state"),
            "market_story_actionable": institutional.get("market_story_actionable"),
            "selected_event": institutional.get("selected_event"),
        },
        "tactical": {
            "candidate": bool(tactical.get("candidate")),
            "passed": bool(tactical.get("passed")),
            "status": tactical.get("status"),
            "playbook": tactical.get("playbook"),
            "reason": tactical.get("reason"),
            "market_story_state": tactical.get("market_story_state"),
            "market_story_actionable": tactical.get("market_story_actionable"),
            "selected_event": tactical.get("selected_event"),
        },
    }


async def _record_scanner_observations(results: list[dict[str, Any]], timeframe: str) -> None:
    """Persist compact failure/success evidence so calibration is measurable."""
    observations = [item.get("scanner_observation") for item in results if item.get("scanner_observation")]
    if not observations:
        return
    try:
        async with AsyncSessionLocal() as db:
            db.add_all([ScannerObservation(**observation) for observation in observations])
            await db.commit()
    except Exception as exc:
        # Scanner operation must not fail merely because historical telemetry
        # could not be written; the current run remains visible in memory.
        logger.warning("Unable to persist scanner observations: %s", exc)


async def get_scanner_diagnostics(limit: int = 120) -> dict[str, Any]:
    """Return recent blocker distribution and tactical/institutional conversion."""
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.scalars(
                select(ScannerObservation)
                .order_by(ScannerObservation.created_at.desc(), ScannerObservation.id.desc())
                .limit(max(1, min(limit, 500)))
            )).all()
    except Exception as exc:
        logger.warning("Unable to load scanner diagnostics: %s", exc)
        return {"available": False, "reason": "scanner_observation_history_unavailable"}

    blocker_counts = Counter(row.primary_blocker for row in rows if row.primary_blocker)
    published = sum(row.status == "SIGNAL_PUBLISHED" for row in rows)
    tactical_watches = sum(bool((row.tactical or {}).get("candidate")) for row in rows)
    return {
        "available": True,
        "observations": len(rows),
        "published": published,
        "tactical_watches": tactical_watches,
        "blocker_counts": [
            {"reason": reason, "count": count}
            for reason, count in blocker_counts.most_common(5)
        ],
        "latest": [
            {
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "status": row.status,
                "primary_blocker": row.primary_blocker,
                "publication_ready": bool(
                    (row.publication_coverage or {}).get(
                        "confirmation_ready",
                        (row.institutional or {}).get("passed", False),
                    )
                ),
                "publication_inputs_complete": bool(
                    (row.publication_coverage or {}).get(
                        "inputs_complete",
                        (row.publication_coverage or {}).get("ready"),
                    )
                ),
                "institutional": row.institutional or {},
                "tactical": row.tactical or {},
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows[:8]
        ],
    }


async def get_scanner_configuration(settings: Any | None = None) -> dict[str, Any]:
    """Load the durable scanner settings, seeding them once from environment defaults."""
    settings = settings or get_settings()
    async with AsyncSessionLocal() as db:
        config = await db.get(ScannerConfiguration, 1)
        if config is None:
            config = ScannerConfiguration(
                id=1,
                enabled=bool(settings.autonomous_scan_enabled),
                discovery=bool(settings.autonomous_pair_discovery),
                watchlist=list(settings.watchlist),
            )
            db.add(config)
            await db.commit()
            await db.refresh(config)
        return {
            "enabled": bool(config.enabled),
            "discovery": bool(config.discovery),
            "watchlist": list(config.watchlist or []),
            "updated_at": config.updated_at.isoformat() if config.updated_at else None,
        }


async def update_scanner_configuration(
    *, enabled: bool | None = None, discovery: bool | None = None, watchlist: list[str] | None = None,
) -> dict[str, Any]:
    """Persist an admin-approved global scanner change and update local status."""
    settings = get_settings()
    await get_scanner_configuration(settings)
    async with AsyncSessionLocal() as db:
        config = await db.get(ScannerConfiguration, 1, with_for_update=True)
        if enabled is not None:
            config.enabled = enabled
        if discovery is not None:
            config.discovery = discovery
        if watchlist is not None:
            config.watchlist = list(watchlist)
        await db.commit()
        await db.refresh(config)
    snapshot = {
        "enabled": bool(config.enabled),
        "discovery": bool(config.discovery),
        "watchlist": list(config.watchlist or []),
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }
    scanner_state["watchlist"] = snapshot["watchlist"]
    return snapshot


async def run_single_symbol_scan(symbol: str, timeframe: str, settings: Any) -> dict[str, Any] | None:
    """Run AI Council for a single symbol and publish signal if approved."""
    try:
        # Check if an active signal already exists to avoid duplicate scans
        active = await get_active_signal(symbol, timeframe)
        if active:
            logger.info(f"Skipping scan for {symbol}/{timeframe} - active signal exists: ID {active.id}")
            return {
                "symbol": symbol,
                "status": "SKIPPED",
                "reason": "Active signal already exists.",
            }

        # Run the council and the exact same completed-candle confirmation
        # gate used by the main research workspace before anything can enter
        # the shared signal ledger.
        intelligence = await fetch_market_intelligence(symbol, timeframe, settings)
        cio_result = await run_ai_council(
            symbol,
            timeframe,
            settings,
            intelligence=intelligence,
            defer_ai_validation=True,
        )
        # Council work can outlive the feed freshness window. Refresh the
        # process-shared evidence immediately before the publication gate so
        # stale flow cannot confirm or veto a signal.
        intelligence = attach_live_execution_tape_snapshot(intelligence, symbol, settings)

        # Build trade setup
        # The compact summary is for display; lifecycle context should use
        # the complete feature snapshot used by the council.
        features = cio_result.get("full_quant_features") or cio_result.get("quant_features", {})
        trade_setup = build_ai_driven_trade_setup(cio_result, features, settings)
        direction_side = "LONG" if cio_result.get("decision") == "BUY_WATCH" else "SHORT" if cio_result.get("decision") == "SELL_WATCH" else None
        confirmation_timeframes = {"1m": "5m", "5m": "1h", "15m": "4h", "1h": "1d", "4h": "1d", "1d": "1w"}
        higher = (intelligence.get("multi_tf_candles", {}) or {}).get(confirmation_timeframes.get(timeframe), [])
        cio_result["live_confirmation"] = await asyncio.to_thread(
            verify_main_signal_snapshot,
            symbol=symbol, timeframe=timeframe, side=direction_side,
            candles=intelligence.get("candles", []), higher_candles=higher,
            order_book=intelligence.get("order_book", {}), funding=intelligence.get("funding", {}),
            derivatives=intelligence.get("derivatives", {}),
            multi_venue=intelligence.get("execution_tape", {}) or {},
            planned_notional_usd=(trade_setup.get("position") or {}).get("notional_usd"),
        )
        # Code-owned gates are cheaper and non-overridable. Do not spend a
        # provider request on a candidate that already failed structure,
        # live-flow, risk, data-quality, or trade-plan controls.
        preliminary_approval = evaluate_ai_driven_approval(
            cio_result,
            trade_setup,
            require_ai_validation=False,
        )
        if preliminary_approval["approved"] and settings.require_ai_for_signal_publication:
            cio_result = await finalize_ai_council_validation(
                cio_result,
                trade_setup,
                cio_result["live_confirmation"],
                settings,
            )
            trade_setup = build_ai_driven_trade_setup(cio_result, features, settings)
            approval = evaluate_ai_driven_approval(
                cio_result,
                trade_setup,
                require_ai_validation=True,
            )
        else:
            approval = preliminary_approval
        observation = _scanner_observation(
            symbol=symbol,
            timeframe=timeframe,
            status="SIGNAL_PUBLISHED" if approval["approved"] else ("WATCH_ONLY" if trade_setup.get("status") == "WATCH_ONLY" else "HOLD"),
            decision=cio_result.get("decision"),
            confidence=cio_result.get("confidence_pct"),
            approval=approval,
            live_confirmation=cio_result.get("live_confirmation"),
        )

        if approval["approved"]:
            logger.info(f"Institutional Committee approved a manual-review signal for {symbol}/{timeframe}.")

            # Prepare signal database and insert the new signal
            await ensure_signal_database()
            async with AsyncSessionLocal() as db:
                await _lock_signal_key(db, symbol, timeframe)
                existing = await db.scalar(
                    select(TradeSignal)
                    .where(TradeSignal.symbol == symbol, TradeSignal.timeframe == timeframe, TradeSignal.status.in_(("PENDING_ENTRY", "ACTIVE", "TP1_SECURED", "TP2_SECURED", "TP3_SECURED")))
                    .order_by(TradeSignal.id.desc())
                    .limit(1)
                )
                if existing:
                    return {"symbol": symbol, "status": "SKIPPED", "reason": "An active signal was published by another worker."}
                current_price = cio_result["current_price"]
                market_context = {
                    "market_story": trade_setup.get("market_story", {}),
                    "execution_tape": intelligence.get("execution_tape", {}) or {},
                    "causal_market_context": features.get("market_context", {}) or {},
                }

                seed = build_signal_seed(
                    symbol=symbol,
                    timeframe=timeframe,
                    decision=cio_result,
                    trade_setup=trade_setup,
                    approval=approval,
                    current_price=current_price,
                    context=market_context,
                    ai_review=cio_result,
                )
                signal = TradeSignal(**seed)
                db.add(signal)
                await db.commit()
                await db.refresh(signal)

                # Send system notification
                try:
                    from app.utils.alerts import trigger_system_notification
                    trigger_system_notification(
                        f"Committee Signal: {symbol} {timeframe}",
                        f"{trade_setup.get('allocation_tier', 'MANUAL_REVIEW')} {signal.side} setup. Entry: {signal.entry_reference}, stop: {signal.stop_initial}, TP1: {(trade_setup.get('targets') or {}).get('tp1_1r', 'N/A')}",
                    )
                except Exception as alert_exc:
                    logger.warning(f"Failed to send alert notification: {alert_exc}")

                return {
                    "symbol": symbol,
                    "status": "SIGNAL_PUBLISHED",
                    "signal_id": signal.id,
                    "side": signal.side,
                    "confidence": signal.confidence,
                    "scanner_observation": observation,
                }
        else:
            return {
                "symbol": symbol,
                "status": "WATCH_ONLY" if trade_setup.get("status") == "WATCH_ONLY" else "HOLD",
                "reason": approval["summary"],
                "decision": cio_result["decision"],
                "confidence": cio_result["confidence_pct"],
                "scanner_observation": observation,
            }

    except Exception as exc:
        logger.error(f"Error scanning {symbol}/{timeframe}: {exc}", exc_info=True)
        return {
            "symbol": symbol,
            "status": "ERROR",
            "reason": str(exc),
        }


async def run_scan_cycle() -> None:
    """Execute a complete scan cycle across watchlist and/or discovered pairs."""
    if scanner_state["is_scanning"]:
        logger.warning("Scan cycle already in progress. Skipping.")
        return

    # PostgreSQL advisory locks make scanner ownership explicit when a Docker
    # deployment has more than one app process. Keep the connection open for
    # the run because session-level advisory locks live on that connection.
    lease_connection = None
    if engine.dialect.name == "postgresql":
        lease_connection = await engine.connect()
        acquired = await lease_connection.scalar(text("SELECT pg_try_advisory_lock(hashtext('atc:autonomous-scanner'))"))
        if not acquired:
            await lease_connection.close()
            logger.info("Another worker owns the autonomous scanner lease; skipping this cycle.")
            return

    scanner_state["is_scanning"] = True
    scanner_state["last_scan_time"] = datetime.now(timezone.utc).isoformat()
    scanner_state["current_symbol"] = None

    settings = get_settings()

    try:
        configuration = await get_scanner_configuration(settings)
        timeframe = "15m"  # Standard default timeframe for scanning
        # Determine symbols to scan
        symbols = list(configuration["watchlist"])
        scanner_state["watchlist"] = symbols

        # Fetch discovered pairs if enabled
        if configuration["discovery"]:
            logger.info("Running autonomous pair discovery...")
            discovery_data = await fetch_pair_discovery_data(settings)
            
            # Extract top 5 volume symbols that end in USDT and aren't in the watchlist
            discovered = []
            for item in discovery_data.get("top_volume_pairs", []):
                symbol = item.get("symbol")
                if symbol and symbol not in symbols and symbol.endswith("USDT"):
                    discovered.append(symbol)
                if len(discovered) >= 3:
                    break
            
            scanner_state["discovered_pairs"] = discovered
            symbols.extend(discovered)
            logger.info(f"Discovered pairs to add: {discovered}")

        logger.info(f"Starting scan cycle for symbols: {symbols}")
        results = []

        for symbol in symbols:
            scanner_state["current_symbol"] = symbol
            res = await run_single_symbol_scan(symbol, timeframe, settings)
            if res:
                results.append(res)
            # Gentle cooldown between symbols to avoid token/rate limits
            await asyncio.sleep(8.0)

        # Save run history
        scanner_state["scan_history"].insert(0, {
            "time": scanner_state["last_scan_time"],
            "results": results,
        })
        # Limit history size
        scanner_state["scan_history"] = scanner_state["scan_history"][:10]
        await _record_scanner_observations(results, timeframe)

    except Exception as exc:
        logger.error(f"Scanner cycle failed: {exc}", exc_info=True)
    finally:
        scanner_state["is_scanning"] = False
        scanner_state["current_symbol"] = None
        
        # Calculate next scan run time
        interval = settings.scan_interval_seconds
        next_run = datetime.now(timezone.utc).timestamp() + interval
        scanner_state["next_scan_time"] = datetime.fromtimestamp(next_run, timezone.utc).isoformat()
        if lease_connection is not None:
            try:
                await lease_connection.execute(text("SELECT pg_advisory_unlock(hashtext('atc:autonomous-scanner'))"))
            finally:
                await lease_connection.close()


async def autonomous_scanner_loop() -> None:
    """Continuous loop running scanner in the background."""
    logger.info("Starting Autonomous Scanner background loop...")
    settings = get_settings()
    
    # Initial delay on startup to let DB initialize
    await asyncio.sleep(10)

    while True:
        try:
            settings = get_settings()
            configuration = await get_scanner_configuration(settings)
            if configuration["enabled"]:
                await run_scan_cycle()
            else:
                # When disabled, this loop used to query the database every ten
                # seconds and emit a log line each time.  An idle scanner should
                # have near-zero CPU, database and log volume.
                await asyncio.sleep(max(15, settings.background_idle_check_seconds))
                continue

            # Sleep for the configured interval (checking periodically if
            # settings changed while the scanner is actually enabled).
            interval = settings.scan_interval_seconds
            slept = 0
            while slept < interval:
                await asyncio.sleep(10)
                slept += 10
                # Re-load settings to check if disabled/interval changed
                settings = get_settings()
                configuration = await get_scanner_configuration(settings)
                if not configuration["enabled"] or settings.scan_interval_seconds != interval:
                    break
        except asyncio.CancelledError:
            logger.info("Autonomous Scanner loop cancelled.")
            raise
        except Exception as e:
            logger.error(f"Error in autonomous_scanner_loop: {e}", exc_info=True)
            await asyncio.sleep(30)
