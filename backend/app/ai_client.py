from dataclasses import dataclass
from typing import Any
from openai import OpenAI, AsyncOpenAI

from .settings import Settings


@dataclass(frozen=True)
class AIRequestConfig:
    """In-memory credentials for one authenticated user's AI request."""
    provider: str
    api_key: str
    model: str


def ai_is_configured(settings: Settings, override: AIRequestConfig | None = None) -> bool:
    if override is not None:
        return bool(override.api_key.strip())
    provider = settings.ai_provider.lower().strip()
    if provider == "openrouter":
        return bool(settings.openrouter_api_key.strip())
    if provider == "openai":
        return bool(settings.openai_api_key.strip())
    if provider == "puter":
        return bool(settings.puter_api_key.strip())
    if provider == "gemini":
        return bool(settings.gemini_api_key.strip())
    if provider == "qwen":
        return bool(settings.qwen_api_key.strip())
    return False


def get_model_for_task(settings: Settings, task: str, override: AIRequestConfig | None = None) -> str:
    if override is not None:
        return override.model
    provider = settings.ai_provider.lower().strip()
    is_judge = task.lower().strip() == "judge"

    if provider == "openrouter":
        return settings.openrouter_model_judge if is_judge else settings.openrouter_model_scanner
    if provider == "openai":
        return settings.openai_model_judge if is_judge else settings.openai_model_scanner
    if provider == "puter":
        return settings.puter_model_judge if is_judge else settings.puter_model_scanner
    if provider == "gemini":
        return settings.gemini_model_judge if is_judge else settings.gemini_model_scanner
    if provider == "qwen":
        return settings.qwen_model_judge if is_judge else settings.qwen_model_scanner

    raise ValueError(f"Unsupported AI provider: {settings.ai_provider}")


def build_ai_client(settings: Settings, override: AIRequestConfig | None = None) -> OpenAI | None:
    if override is not None:
        if override.provider.lower().strip() != "openrouter" or not override.api_key.strip():
            return None
        return OpenAI(
            base_url=settings.openrouter_base_url, api_key=override.api_key, max_retries=0,
            default_headers={"HTTP-Referer": settings.openrouter_http_referer, "X-OpenRouter-Title": settings.openrouter_app_title},
        )
    provider = settings.ai_provider.lower().strip()

    if provider == "openrouter":
        if not settings.openrouter_api_key.strip():
            return None
        return OpenAI(
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            max_retries=0,
            default_headers={
                "HTTP-Referer": settings.openrouter_http_referer,
                "X-OpenRouter-Title": settings.openrouter_app_title,
            },
        )

    if provider == "openai":
        if not settings.openai_api_key.strip():
            return None
        return OpenAI(api_key=settings.openai_api_key)
        
    if provider == "puter":
        if not settings.puter_api_key.strip():
            return None
        return OpenAI(
            base_url="https://api.puter.com/puterai/openai/v1/",
            api_key=settings.puter_api_key,
            max_retries=0,
        )

    if provider == "gemini":
        if not settings.gemini_api_key.strip():
            return None
        return OpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=settings.gemini_api_key,
            max_retries=0,
        )

    if provider == "qwen":
        if not settings.qwen_api_key.strip():
            return None
        return OpenAI(
            base_url=settings.qwen_base_url,
            api_key=settings.qwen_api_key,
            max_retries=0,
        )

    raise ValueError(f"Unsupported AI provider: {settings.ai_provider}")


def build_async_ai_client(settings: Settings, override: AIRequestConfig | None = None) -> AsyncOpenAI | None:
    if override is not None:
        if override.provider.lower().strip() != "openrouter" or not override.api_key.strip():
            return None
        return AsyncOpenAI(
            base_url=settings.openrouter_base_url, api_key=override.api_key, max_retries=0,
            default_headers={"HTTP-Referer": settings.openrouter_http_referer, "X-OpenRouter-Title": settings.openrouter_app_title},
        )
    provider = settings.ai_provider.lower().strip()

    if provider == "openrouter":
        if not settings.openrouter_api_key.strip():
            return None
        return AsyncOpenAI(
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            max_retries=0,
            default_headers={
                "HTTP-Referer": settings.openrouter_http_referer,
                "X-OpenRouter-Title": settings.openrouter_app_title,
            },
        )

    if provider == "openai":
        if not settings.openai_api_key.strip():
            return None
        return AsyncOpenAI(api_key=settings.openai_api_key)

    if provider == "puter":
        if not settings.puter_api_key.strip():
            return None
        return AsyncOpenAI(
            base_url="https://api.puter.com/puterai/openai/v1/",
            api_key=settings.puter_api_key,
            max_retries=0,
        )

    if provider == "gemini":
        if not settings.gemini_api_key.strip():
            return None
        return AsyncOpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=settings.gemini_api_key,
            max_retries=0,
        )

    if provider == "qwen":
        if not settings.qwen_api_key.strip():
            return None
        return AsyncOpenAI(
            base_url=settings.qwen_base_url,
            api_key=settings.qwen_api_key,
            max_retries=0,
        )

    raise ValueError(f"Unsupported AI provider: {settings.ai_provider}")


_UNSUPPORTED_RESPONSE_FORMAT_MODELS: set[str] = set()


async def safe_async_chat_completion(
    client: Any,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    response_format: dict[str, str] | None = None,
    max_retries: int = 3,
) -> Any:
    """Robust async chat completion wrapper.

    1. If provider is known to reject response_format (or fails once),
       removes response_format before sending to avoid wasteful 400 retries.
    2. If provider returns 429 Rate Limit, backs off with sleep and retries up to max_retries.
    """
    import asyncio
    import logging

    logger = logging.getLogger(__name__)

    current_kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None and model not in _UNSUPPORTED_RESPONSE_FORMAT_MODELS:
        current_kwargs["response_format"] = response_format

    for attempt in range(max_retries + 1):
        try:
            return await client.chat.completions.create(**current_kwargs)
        except Exception as exc:
            err_msg = str(exc)

            # Check for unsupported response_format (400)
            if "response_format" in current_kwargs and (
                "json_object" in err_msg.lower()
                or "response format" in err_msg.lower()
                or "supported formats" in err_msg.lower()
                or "400" in err_msg
            ):
                logger.info(
                    f"Model '{model}' does not support response_format. Memorizing model to skip response_format on future calls."
                )
                _UNSUPPORTED_RESPONSE_FORMAT_MODELS.add(model)
                current_kwargs.pop("response_format", None)
                continue

            # Check for Rate Limit (429)
            if "429" in err_msg or "rate limit" in err_msg.lower() or "too many requests" in err_msg.lower():
                if attempt < max_retries:
                    import random
                    wait_sec = 3.0 * (attempt + 1) + random.uniform(1.0, 3.0)
                    logger.warning(
                        f"Rate limit 429 for '{model}'. Backing off for {wait_sec:.1f}s (attempt {attempt + 1}/{max_retries})..."
                    )
                    await asyncio.sleep(wait_sec)
                    continue

            # Re-raise if no retry condition met or max retries exhausted
            raise
