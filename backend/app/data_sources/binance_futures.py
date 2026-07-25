import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)

class BinanceFuturesClient:
    def __init__(self, base_url: str = "https://fapi.binance.com", timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def get_funding_rate(self, symbol: str) -> dict[str, Any]:
        """
        Fetch latest funding rate for a symbol.
        Endpoint: GET /fapi/v1/fundingRate?symbol=BTCUSDT&limit=1
        """
        try:
            data = await self._get("/fapi/v1/fundingRate", {"symbol": symbol.upper(), "limit": 1})
            if data and isinstance(data, list):
                return {
                    "funding_rate": float(data[0].get("fundingRate", 0.0)),
                    "funding_time": int(data[0].get("fundingTime", 0)),
                }
            return {"funding_rate": 0.0, "funding_time": 0}
        except Exception as e:
            logger.error(f"Error fetching funding rate for {symbol}: {e}")
            return {"funding_rate": 0.0, "funding_time": 0, "error": str(e)}

    async def get_open_interest(self, symbol: str) -> dict[str, Any]:
        """
        Fetch open interest for a symbol.
        Endpoint: GET /fapi/v1/openInterest?symbol=BTCUSDT
        """
        try:
            data = await self._get("/fapi/v1/openInterest", {"symbol": symbol.upper()})
            return {
                "open_interest": float(data.get("openInterest", 0.0)),
                "time": int(data.get("time", 0)),
            }
        except Exception as e:
            logger.error(f"Error fetching open interest for {symbol}: {e}")
            return {"open_interest": 0.0, "time": 0, "error": str(e)}
