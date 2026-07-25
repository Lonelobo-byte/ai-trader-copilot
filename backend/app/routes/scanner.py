"""Scanner management routes."""
from __future__ import annotations

import asyncio
import logging
from typing import List
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.autonomous_scanner import scanner_state, run_scan_cycle
from app.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scanner", tags=["scanner"])


class WatchlistUpdate(BaseModel):
    symbols: List[str]


class ScannerToggle(BaseModel):
    enabled: bool | None = None
    discovery: bool | None = None


@router.get("/status")
def get_scanner_status():
    """Get the current state and run history of the autonomous scanner."""
    settings = get_settings()
    # Merge config states into status dict
    payload = dict(scanner_state)
    payload["autonomous_scan_enabled"] = settings.autonomous_scan_enabled
    payload["autonomous_pair_discovery"] = settings.autonomous_pair_discovery
    return payload


@router.post("/toggle")
def toggle_scanner(data: ScannerToggle):
    """Toggle scanner loop and AI discovery configurations dynamically."""
    settings = get_settings()
    if data.enabled is not None:
        settings.autonomous_scan_enabled = data.enabled
        logger.info(f"Autonomous background scanner loop state toggled to: {data.enabled}")
    if data.discovery is not None:
        settings.autonomous_pair_discovery = data.discovery
        logger.info(f"Autonomous AI pair discovery state toggled to: {data.discovery}")
    return {
        "status": "success",
        "autonomous_scan_enabled": settings.autonomous_scan_enabled,
        "autonomous_pair_discovery": settings.autonomous_pair_discovery
    }


@router.post("/trigger")
def trigger_scanner(background_tasks: BackgroundTasks):
    """Manually trigger a full scan cycle in the background."""
    if scanner_state["is_scanning"]:
        return {"status": "busy", "message": "Scanner is already running."}
    
    background_tasks.add_task(run_scan_cycle)
    return {"status": "triggered", "message": "Autonomous scan cycle triggered in background."}


@router.get("/watchlist")
def get_watchlist():
    """Get the currently configured watchlist."""
    settings = get_settings()
    return {"watchlist": settings.watchlist}


@router.post("/watchlist")
def update_watchlist(data: WatchlistUpdate):
    """Update the scanner watchlist."""
    settings = get_settings()
    symbols = [s.upper().strip() for s in data.symbols if s.strip()]
    settings.watchlist = symbols
    scanner_state["watchlist"] = symbols
    logger.info(f"Watchlist updated to: {symbols}")
    return {"status": "success", "watchlist": settings.watchlist}
