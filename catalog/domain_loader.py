from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from catalog.domain_models import DomainProfile


DEFAULT_DOMAIN_ID = "olist"
DOMAINS_DIR = Path(__file__).resolve().parent / "domains"


def load_domain_profile(profile: str | Path | None = None) -> DomainProfile:
    path = _resolve_profile_path(profile)
    return _load_domain_profile_from_path(str(path))


def try_load_domain_profile(profile: str | Path | None = None) -> DomainProfile | None:
    try:
        return load_domain_profile(profile)
    except Exception:
        return None


def _resolve_profile_path(profile: str | Path | None) -> Path:
    explicit_path = os.getenv("DOMAIN_PROFILE_PATH")
    value = str(profile or explicit_path or os.getenv("DOMAIN_PROFILE") or DEFAULT_DOMAIN_ID).strip()
    if not value:
        value = DEFAULT_DOMAIN_ID

    path = Path(value)
    if path.exists():
        return path
    if path.suffix:
        return path
    return DOMAINS_DIR / f"{value}.json"


@lru_cache(maxsize=16)
def _load_domain_profile_from_path(path_value: str) -> DomainProfile:
    path = Path(path_value)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Domain profile must be a JSON object.")
    return DomainProfile.from_dict(data)
