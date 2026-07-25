"""AI multi-agent coordinators.

Executes structured queries to specialized analyst models using settings
and configurations, loading system instructions from external prompt files.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.ai_client import build_async_ai_client, get_model_for_task, safe_async_chat_completion
from app.brains.prompts.loader import load_prompt
from app.settings import Settings

logger = logging.getLogger(__name__)


async def _call_agent(system_prompt: str, user_prompt: str, settings: Settings, task: str = "scanner") -> dict[str, Any]:
    client = build_async_ai_client(settings)
    if not client:
        return {"error": "AI client not configured."}

    model = get_model_for_task(settings, task)
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            # Stagger startup of parallel agents to spread load
            if attempt == 1:
                import random
                await asyncio.sleep(random.uniform(0.05, 1.5))

            completion = await safe_async_chat_completion(
                client=client,
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            content = completion.choices[0].message.content or "{}"
            from app.utils.json_helper import loads_repaired
            return loads_repaired(content)
        except Exception as exc:
            is_quota_exhausted = "free-models-per-day" in str(exc) or "quota exceeded" in str(exc).lower() or "credit limit" in str(exc).lower()
            if is_quota_exhausted:
                logger.error(f"OpenRouter Daily Free Limit Exceeded: {exc}. Please add credits to your OpenRouter account or switch to a paid model.")
                return {"error": "OpenRouter free-tier daily request limit exceeded."}

            is_rate_limit = "429" in str(exc) or "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower()
            if is_rate_limit and attempt < max_attempts:
                import random
                sleep_sec = (5 * attempt) + random.uniform(3.0, 7.0)
                logger.warning(f"Agent ({task}) rate limited (429). Retrying in {sleep_sec:.2f}s... (Attempt {attempt}/{max_attempts})")
                await asyncio.sleep(sleep_sec)
                continue

            logger.error(f"Agent ({task}) failed: {exc}", exc_info=True)
            return {"error": str(exc)}


async def run_tech_analyst(symbol: str, timeframe: str, candles: list, ticker: dict, sweep: dict, settings: Settings) -> dict:
    sys_prompt = load_prompt("tech_analyst")
    recent_closes = [c.close for c in candles[-10:]]
    user_prompt = f"Symbol: {symbol} ({timeframe})\nTrend: {ticker.get('trend_status')}\nRecent closes: {recent_closes}\nSweep detected: {sweep.get('detected')} (Direction: {sweep.get('direction')})"
    return await _call_agent(sys_prompt, user_prompt, settings)


async def run_order_flow_analyst(symbol: str, timeframe: str, order_book: dict, settings: Settings) -> dict:
    sys_prompt = load_prompt("order_flow_analyst")
    user_prompt = f"Symbol: {symbol} ({timeframe})\nOrder Book: {json.dumps(order_book)}"
    return await _call_agent(sys_prompt, user_prompt, settings)


async def run_macro_analyst(symbol: str, timeframe: str, macro_data: dict, settings: Settings) -> dict:
    sys_prompt = load_prompt("macro_analyst")
    user_prompt = f"Symbol: {symbol} ({timeframe})\nMacro Data (DXY/NQ/GC/TNX): {json.dumps(macro_data)}"
    return await _call_agent(sys_prompt, user_prompt, settings)


async def run_news_analyst(symbol: str, timeframe: str, news_articles: list, settings: Settings) -> dict:
    sys_prompt = load_prompt("news_analyst")
    articles_str = "\n".join([f"- {a['title']} ({a['source']})" for a in news_articles]) if news_articles else "No news"
    user_prompt = f"Symbol: {symbol}\nArticles:\n{articles_str}"
    return await _call_agent(sys_prompt, user_prompt, settings)


async def run_devils_advocate(symbol: str, reports: dict, settings: Settings) -> dict:
    sys_prompt = load_prompt("devils_advocate")
    user_prompt = f"Symbol: {symbol}\nCurrent Agent Reports: {json.dumps(reports)}"
    return await _call_agent(sys_prompt, user_prompt, settings)


async def run_risk_manager(symbol: str, risk_idea: dict, settings: Settings) -> dict:
    sys_prompt = load_prompt("risk_manager")
    user_prompt = f"Symbol: {symbol}\nRisk Idea: {json.dumps(risk_idea)}"
    return await _call_agent(sys_prompt, user_prompt, settings)


async def run_pre_mortem_analyst(symbol: str, reports: dict, settings: Settings) -> dict:
    sys_prompt = load_prompt("pre_mortem_analyst")
    user_prompt = f"Symbol: {symbol}\nCurrent Agent Reports: {json.dumps(reports)}"
    return await _call_agent(sys_prompt, user_prompt, settings)


async def run_cio(symbol: str, timeframe: str, all_reports: dict, settings: Settings) -> dict:
    sys_prompt = load_prompt("cio")
    user_prompt = f"Symbol: {symbol} ({timeframe})\nAgent Dossier: {json.dumps(all_reports)}"
    return await _call_agent(sys_prompt, user_prompt, settings, task="judge")
