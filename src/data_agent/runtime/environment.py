"""Project environment provider for installable Runtime consumers."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


@lru_cache(maxsize=1)
def load_project_environment() -> bool:
    """Load the current project's ``.env`` once during synchronous startup."""

    return load_dotenv(Path.cwd() / ".env")


__all__ = ["load_project_environment"]
