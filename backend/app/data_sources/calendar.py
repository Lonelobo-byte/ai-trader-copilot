"""Economic calendar data source — TradingView API with environment-gated fallback.

In production (``app_env != 'local'``), only real events from the TradingView
calendar API are returned.  Simulated/mock events are only injected when
``app_env`` is ``'local'`` or ``'test'`` to facilitate UI development.
"""
import logging
from datetime import datetime, timedelta, timezone
from .http_client import get_http_client

logger = logging.getLogger(__name__)


async def fetch_economic_events(app_env: str = "local") -> list[dict]:
    """Fetch upcoming US economic events.

    Parameters
    ----------
    app_env:
        The application environment identifier (e.g. ``"local"``,
        ``"production"``).  Simulated/mock events are **only** injected
        when ``app_env`` is ``"local"`` or ``"test"``.
    """
    url = "https://economic-calendar.tradingview.com/events"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/"
    }
    
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start_time = (now - timedelta(hours=1)).isoformat() + "Z"
    end_time = (now + timedelta(hours=24)).isoformat() + "Z"
    
    params = {
        "from": start_time,
        "to": end_time,
        "countries": "US"
    }

    is_dev = app_env in ("local", "test")

    try:
        client = await get_http_client()
        r = await client.get(url, headers=headers, params=params, timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            events = []
            for event in data.get("result", []):
                importance = event.get("importance", 0)
                imp_str = "LOW"
                if importance == 0:
                    imp_str = "MEDIUM"
                elif importance == 1:
                    imp_str = "HIGH"

                dt_str = event.get("date")
                events.append({
                    "title": event.get("title"),
                    "country": "US",
                    "importance": imp_str,
                    "time": dt_str
                })
            if events:
                logger.info(f"Successfully fetched {len(events)} events from TradingView.")
                # Only inject simulated HIGH event in dev/test environments
                if is_dev and not any(e["importance"] == "HIGH" for e in events):
                    events.append({
                        "title": "US Core CPI MoM (Simulated)",
                        "country": "US",
                        "importance": "HIGH",
                        "time": (now + timedelta(minutes=45)).isoformat() + "Z"
                    })
                return events
    except Exception as e:
        logger.warning(f"Failed to fetch live calendar from TradingView: {e}.")

    # ── Fallback ──────────────────────────────────────────────────────────────
    # In production, return an empty list rather than fake data that could
    # trigger the macro blockout and suppress legitimate signals.
    if not is_dev:
        logger.info("Calendar API unavailable in production; returning empty event list.")
        return []

    # Simulated fallback for local/test environments only
    logger.info("Using simulated economic calendar fallback (dev mode).")
    simulated_events = [
        {
            "title": "US Core CPI YoY (Inflation Rate)",
            "country": "US",
            "importance": "HIGH",
            "time": (now + timedelta(minutes=45)).isoformat() + "Z"
        },
        {
            "title": "Initial Jobless Claims",
            "country": "US",
            "importance": "MEDIUM",
            "time": (now + timedelta(hours=3)).isoformat() + "Z"
        },
        {
            "title": "FOMC Interest Rate Decision",
            "country": "US",
            "importance": "HIGH",
            "time": (now + timedelta(hours=6)).isoformat() + "Z"
        }
    ]
    return simulated_events
