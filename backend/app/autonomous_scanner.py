"""Autonomous Scanner background task — scans the market on a schedule.

Saves high-conviction signals to the database and tracks active scan state.
Supports both a fixed watchlist and dynamic AI pair discovery.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.brains.council import run_ai_council
from app.brains.signal_builder import build_ai_driven_trade_setup, evaluate_ai_driven_approval
from app.data_sources.data_aggregator import fetch_pair_discovery_data
from app.db.database import AsyncSessionLocal
from app.db.models import ScannerConfiguration, TradeSignal
from app.signal_service import get_active_signal, ensure_signal_database
from app.brains.signal_lifecycle import build_signal_seed
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

        # Run full AI Council deliberation
        cio_result = await run_ai_council(symbol, timeframe, settings)

        # Build trade setup
        # The compact summary is for display; lifecycle context should use
        # the complete feature snapshot used by the council.
        features = cio_result.get("full_quant_features") or cio_result.get("quant_features", {})
        trade_setup = build_ai_driven_trade_setup(cio_result, features, settings)

        # Evaluate approval
        approval = evaluate_ai_driven_approval(cio_result, trade_setup)

        if approval["approved"]:
            logger.info(f"Institutional Committee approved a manual-review signal for {symbol}/{timeframe}.")

            # Prepare signal database and insert the new signal
            await ensure_signal_database()
            async with AsyncSessionLocal() as db:
                current_price = cio_result["current_price"]
                market_context = {
                    "trend_status": features.get("trend", {}).get("primary", {}).get("status"),
                    "momentum_bias": features.get("momentum", {}).get("summary", {}).get("bias"),
                    "order_book_pressure": features.get("order_book", {}).get("pressure"),
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
                }
        else:
            return {
                "symbol": symbol,
                "status": "WATCH_ONLY" if trade_setup.get("status") == "WATCH_ONLY" else "HOLD",
                "reason": approval["summary"],
                "decision": cio_result["decision"],
                "confidence": cio_result["confidence_pct"],
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

    scanner_state["is_scanning"] = True
    scanner_state["last_scan_time"] = datetime.now(timezone.utc).isoformat()
    scanner_state["current_symbol"] = None

    settings = get_settings()
    configuration = await get_scanner_configuration(settings)
    timeframe = "15m"  # Standard default timeframe for scanning

    try:
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

    except Exception as exc:
        logger.error(f"Scanner cycle failed: {exc}", exc_info=True)
    finally:
        scanner_state["is_scanning"] = False
        scanner_state["current_symbol"] = None
        
        # Calculate next scan run time
        interval = settings.scan_interval_seconds
        next_run = datetime.now(timezone.utc).timestamp() + interval
        scanner_state["next_scan_time"] = datetime.fromtimestamp(next_run, timezone.utc).isoformat()


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
                logger.info("Autonomous scan disabled in settings. Skipping.")
                
            # Sleep for the configured interval (checking every 10s if settings changed)
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
