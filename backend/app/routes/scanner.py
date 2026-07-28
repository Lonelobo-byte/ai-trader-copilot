"""Scanner management routes."""
from __future__ import annotations

import logging
from typing import List
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from app.auth import require_admin
from app.autonomous_scanner import (
    get_scanner_configuration,
    scanner_state,
    run_scan_cycle,
    update_scanner_configuration,
)
from app.db.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scanner", tags=["scanner"])


class WatchlistUpdate(BaseModel):
    symbols: List[str]


class ScannerToggle(BaseModel):
    enabled: bool | None = None
    discovery: bool | None = None


@router.get("/status")
async def get_scanner_status():
    """Get the current state and run history of the autonomous scanner."""
    config = await get_scanner_configuration()
    payload = dict(scanner_state)
    payload["autonomous_scan_enabled"] = config["enabled"]
    payload["autonomous_pair_discovery"] = config["discovery"]
    payload["watchlist"] = config["watchlist"]
    payload["configuration_updated_at"] = config["updated_at"]
    return payload


@router.post("/toggle")
async def toggle_scanner(data: ScannerToggle, _: User = Depends(require_admin)):
    """Toggle scanner loop and AI discovery configurations dynamically."""
    config = await update_scanner_configuration(enabled=data.enabled, discovery=data.discovery)
    logger.info("Autonomous scanner configuration updated: enabled=%s discovery=%s", config["enabled"], config["discovery"])
    return {
        "status": "success",
        "autonomous_scan_enabled": config["enabled"],
        "autonomous_pair_discovery": config["discovery"],
    }


@router.post("/trigger")
def trigger_scanner(background_tasks: BackgroundTasks, _: User = Depends(require_admin)):
    """Manually trigger a full scan cycle in the background."""
    if scanner_state["is_scanning"]:
        return {"status": "busy", "message": "Scanner is already running."}
    
    background_tasks.add_task(run_scan_cycle)
    return {"status": "triggered", "message": "Autonomous scan cycle triggered in background."}


@router.get("/watchlist")
async def get_watchlist():
    """Get the currently configured watchlist."""
    return {"watchlist": (await get_scanner_configuration())["watchlist"]}


@router.post("/watchlist")
async def update_watchlist(data: WatchlistUpdate, _: User = Depends(require_admin)):
    """Update the scanner watchlist."""
    symbols = [s.upper().strip() for s in data.symbols if s.strip()]
    config = await update_scanner_configuration(watchlist=symbols)
    logger.info(f"Watchlist updated to: {symbols}")
    return {"status": "success", "watchlist": config["watchlist"]}
