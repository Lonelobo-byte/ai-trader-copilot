from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from .http_client import get_http_client

logger = logging.getLogger(__name__)

# In-memory cache for GDELT news to reduce rate-limit issues: {symbol: {"timestamp": float, "articles": list[dict]}}
_news_cache: dict[str, dict[str, Any]] = {}
_NEWS_CACHE_MAX_ENTRIES = 256
CACHE_EXPIRY_SECONDS = 300  # 5 minutes cache for successful requests
STALE_CACHE_EXPIRY_SECONDS = 1800  # 30 minutes stale cache fallback during errors/rate limits


def get_news_query(symbol: str) -> str:
    symbol = symbol.upper().strip()
    base = symbol
    for quote in ["USDT", "BUSD", "USD", "BTC", "ETH"]:
        if symbol.endswith(quote) and symbol != quote:
            base = symbol[: -len(quote)]
            break

    names = {
        "BTC": '("BTC" OR "Bitcoin")',
        "ETH": '("ETH" OR "Ethereum")',
        "SOL": '("SOL" OR "Solana")',
        "ADA": '("ADA" OR "Cardano")',
        "XRP": '("XRP" OR "Ripple")',
        "DOT": '("DOT" OR "Polkadot")',
        "DOGE": '("DOGE" OR "Dogecoin")',
    }
    return names.get(base, f'("{base}")')


async def fetch_raw_gdelt(
    symbol: str,
    gdelt_api_url: str = "https://api.gdeltproject.org/api/v2/doc/doc",
) -> list[dict[str, Any]]:
    """Fetch raw search data from GDELT Document API."""
    query = get_news_query(symbol)
    params = {
        "query": query,
        "mode": "artlist",
        "timespan": "24H",
        "maxrecords": 5,
        "format": "json",
    }
    try:
        client = await get_http_client()
        response = await client.get(gdelt_api_url, params=params, timeout=6.0)
            
        if response.status_code == 429:
            logger.warning(f"GDELT news API rate limit hit (429) for {symbol}.")
            return []
            
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        return [
            {
                "title": art.get("title", "No Title"),
                "url": art.get("url", "#"),
                "source": art.get("sourcecountry") or art.get("domain") or "GDELT",
                "date": art.get("seendate", ""),
            }
            for art in articles
        ]
    except Exception as exc:
        logger.warning(f"GDELT news fetch failed for {symbol}: {exc}")
        return []


async def fetch_gdelt_news(
    symbol: str,
    gdelt_api_url: str = "https://api.gdeltproject.org/api/v2/doc/doc",
) -> list[dict[str, Any]]:
    """Fetch recent market sentiment news by gathering GDELT and RSS news concurrently."""
    symbol = symbol.upper().strip()
    now = time.time()
    
    # Check cache first
    cached = _news_cache.get(symbol)
    if cached:
        age = now - cached["timestamp"]
        if age < CACHE_EXPIRY_SECONDS:
            logger.debug(f"Returning cached GDELT + RSS news for {symbol} (age: {age:.1f}s)")
            return cached["articles"]

    # Gather GDELT and CoinTelegraph RSS in parallel using asyncio.gather
    import asyncio
    
    gdelt_timeout_task = asyncio.wait_for(fetch_raw_gdelt(symbol, gdelt_api_url), timeout=1.25)
    # RSS is optional context and must finish inside the aggregator's news
    # budget. Previously its 8-second client timeout was wrapped only by the
    # outer news timeout, making cold research wait for a known
    # cancellation path instead of returning the sources that completed.
    rss_task = asyncio.wait_for(fetch_rss_news(symbol), timeout=1.25)
    
    results = await asyncio.gather(
        gdelt_timeout_task,
        rss_task,
        return_exceptions=True
    )
    
    gdelt_res = results[0]
    if isinstance(gdelt_res, Exception):
        logger.warning(f"GDELT news fetch failed or timed out for {symbol}: {gdelt_res}")
        gdelt_articles = []
    else:
        gdelt_articles = gdelt_res
        
    rss_res = results[1]
    if isinstance(rss_res, Exception):
        logger.error(f"RSS news fetch failed for {symbol}: {rss_res}")
        rss_articles = []
    else:
        rss_articles = rss_res

    # Tag feeds origins and merge
    combined_articles = []
    
    for art in gdelt_articles:
        art["feed"] = "GDELT"
        combined_articles.append(art)
        
    for art in rss_articles:
        art["feed"] = "RSS"
        combined_articles.append(art)
        
    # Save to cache
    if len(_news_cache) >= _NEWS_CACHE_MAX_ENTRIES and symbol not in _news_cache:
        oldest = min(_news_cache, key=lambda item: _news_cache[item]["timestamp"])
        _news_cache.pop(oldest, None)
    _news_cache[symbol] = {
        "timestamp": now,
        "articles": combined_articles
    }
    return combined_articles


async def fetch_rss_news(symbol: str) -> list[dict[str, Any]]:
    """Fetch and parse CoinTelegraph RSS feed for specific token news."""
    import xml.etree.ElementTree as ET
    symbol = symbol.upper().strip()
    
    # Extract base symbol (e.g. BTC from BTCUSDT)
    base = symbol
    for quote in ["USDT", "BUSD", "USD", "BTC", "ETH"]:
        if symbol.endswith(quote) and symbol != quote:
            base = symbol[: -len(quote)]
            break

    # Setup matching keyword list
    keywords = [base.lower()]
    if base == "BTC":
        keywords.extend(["bitcoin", "btc"])
    elif base == "ETH":
        keywords.extend(["ethereum", "eth"])
    elif base == "SOL":
        keywords.extend(["solana", "sol"])
    elif base == "ADA":
        keywords.extend(["cardano", "ada"])
    elif base == "XRP":
        keywords.extend(["ripple", "xrp"])
    elif base == "DOT":
        keywords.extend(["polkadot", "dot"])
    elif base == "DOGE":
        keywords.extend(["dogecoin", "doge"])

    url = "https://cointelegraph.com/rss"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        client = await get_http_client()
        resp = await client.get(url, headers=headers, timeout=8.0)
        if resp.status_code != 200:
            return []
            
        root = ET.fromstring(resp.text)
        channel = root.find("channel")
        if channel is None:
            return []
            
        items = channel.findall("item")
        matched = []
        general = []
        
        for item in items:
            title_node = item.find("title")
            title_text = title_node.text if title_node is not None else ""
            desc_node = item.find("description")
            desc_text = desc_node.text if desc_node is not None else ""
            link_node = item.find("link")
            link_text = link_node.text if link_node is not None else "#"
            pub_date = item.find("pubDate")
            date_text = pub_date.text if pub_date is not None else ""
            
            article = {
                "title": title_text,
                "url": link_text,
                "source": "CoinTelegraph",
                "date": date_text
            }
            
            # Check if article matches search criteria
            matches = any(kw in title_text.lower() or kw in desc_text.lower() for kw in keywords)
            if matches:
                matched.append(article)
            general.append(article)
            
        # Return matching articles first; if empty, return general top news
        # If it's a global query, directly bypass keyword filtering and return all
        if symbol == "CRYPTO_GLOBAL":
            return general[:5]
            
        final_articles = matched if matched else general
        return final_articles[:5]
    except Exception as e:
        logger.warning(f"Failed to fetch RSS news feed: {e}")
        return []


async def fetch_global_news(
    gdelt_api_url: str = "https://api.gdeltproject.org/api/v2/doc/doc",
) -> list[dict[str, Any]]:
    """Fetch global industry cryptocurrency news headlines in parallel."""
    gdelt_timeout_task = asyncio.wait_for(fetch_raw_gdelt("crypto", gdelt_api_url), timeout=1.25)
    rss_task = asyncio.wait_for(fetch_rss_news("crypto_global"), timeout=1.25)
    
    results = await asyncio.gather(
        gdelt_timeout_task,
        rss_task,
        return_exceptions=True
    )
    
    gdelt_res = results[0]
    if isinstance(gdelt_res, Exception):
        logger.warning(f"GDELT global news fetch failed or timed out: {gdelt_res}")
        gdelt_articles = []
    else:
        gdelt_articles = gdelt_res
        
    rss_res = results[1]
    if isinstance(rss_res, Exception):
        logger.error(f"RSS global news fetch failed: {rss_res}")
        rss_articles = []
    else:
        rss_articles = rss_res
        
    combined = []
    for art in gdelt_articles:
        art["feed"] = "GDELT"
        combined.append(art)
    for art in rss_articles:
        art["feed"] = "RSS"
        combined.append(art)
        
    return combined
