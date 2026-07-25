"""Dynamic prompt loader for AI agents.

Reads prompt configurations from markdown files in the prompts directory,
providing in-memory caching and clean error handling.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=16)
def load_prompt(agent_name: str) -> str:
    """Read the prompt contents from the corresponding markdown file.

    Parameters
    ----------
    agent_name : str
        The filename prefix for the prompt (e.g. 'tech_analyst').
    """
    file_path = PROMPTS_DIR / f"{agent_name}.md"
    if not file_path.exists():
        raise FileNotFoundError(f"Prompt file not found for agent '{agent_name}' at: {file_path}")
    return file_path.read_text(encoding="utf-8").strip()


def clear_prompt_cache() -> None:
    """Clear the cached prompt contents in memory."""
    load_prompt.cache_clear()
