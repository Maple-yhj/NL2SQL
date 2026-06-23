from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


@lru_cache(maxsize=1)
def load_project_environment() -> bool:
    """Load the project .env once during synchronous startup/import."""

    project_root = Path(__file__).resolve().parents[1]
    return load_dotenv(project_root / ".env")
