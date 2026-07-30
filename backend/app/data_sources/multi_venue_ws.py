"""Compatibility exports for the Binance/Bybit execution-tape collector."""

from app.data_sources.execution_tape_ws import (
    BookIntegrityError,
    ExecutionTapeHub,
    SubscriptionError,
    execution_tape_market_data_loop,
    get_execution_tape_hub,
    get_execution_tape_snapshot,
)

MultiVenueMarketDataHub = ExecutionTapeHub
get_multi_venue_hub = get_execution_tape_hub
get_multi_venue_snapshot = get_execution_tape_snapshot
multi_venue_market_data_loop = execution_tape_market_data_loop

__all__ = [
    "BookIntegrityError",
    "ExecutionTapeHub",
    "MultiVenueMarketDataHub",
    "SubscriptionError",
    "execution_tape_market_data_loop",
    "get_execution_tape_hub",
    "get_execution_tape_snapshot",
    "get_multi_venue_hub",
    "get_multi_venue_snapshot",
    "multi_venue_market_data_loop",
]
