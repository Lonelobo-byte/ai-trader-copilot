import logging
import asyncio

logger = logging.getLogger(__name__)

async def fetch_macro_data() -> dict:
    """Fetches DXY (US Dollar), NQ=F (Nasdaq), GC=F (Gold), and ^TNX (10Y Yield)."""
    # Use asyncio.to_thread because yfinance is synchronous and blocks
    try:
        def fetch():
            # yfinance imports pandas and a sizeable numerical stack.  Loading
            # it at module import made every API/Radar-only process pay that
            # memory and startup cost even when macro data was never requested.
            import yfinance as yf

            tickers = yf.Tickers("DX-Y.NYB NQ=F GC=F ^TNX")
            data = {}
            for sym, t in tickers.tickers.items():
                try:
                    # Query 5 days of history to guarantee we get the latest trading data on weekends
                    hist = t.history(period="5d")
                    if not hist.empty:
                        last_row = hist.iloc[-1]
                        close_val = float(last_row["Close"])
                        open_val = float(last_row["Open"])
                        
                        change = 0.0
                        if open_val != 0:
                            change = ((close_val - open_val) / open_val) * 100
                            
                        data[sym] = {
                            "close": round(close_val, 4),
                            "change_pct": round(change, 4)
                        }
                    else:
                        data[sym] = {"close": None, "change_pct": None}
                except Exception as e:
                    logger.warning(f"Failed to fetch macro {sym}: {e}")
                    data[sym] = {"close": None, "change_pct": None}
            return data

        macro_data = await asyncio.to_thread(fetch)
        
        return {
            "DXY (Dollar Index)": macro_data.get("DX-Y.NYB", {}),
            "NASDAQ Futures": macro_data.get("NQ=F", {}),
            "Gold Futures": macro_data.get("GC=F", {}),
            "10Y Treasury Yield": macro_data.get("^TNX", {})
        }
    except Exception as exc:
        logger.error(f"Failed to fetch macro data: {exc}")
        return {"error": "Macro data unavailable"}
