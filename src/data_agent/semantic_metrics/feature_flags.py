"""Environment-backed rollout controls for semantic metric governance."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _enabled(environment: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean feature flag")


@dataclass(frozen=True, slots=True)
class SemanticMetricFeatures:
    domain_pack_discovery: bool = True
    web_discovery: bool = False
    provisional_overlays: bool = False
    auto_publish_alias: bool = False

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "SemanticMetricFeatures":
        env = os.environ if environment is None else environment
        return cls(
            domain_pack_discovery=_enabled(
                env, "SEMANTIC_METRICS_DOMAIN_DISCOVERY", True
            ),
            web_discovery=_enabled(
                env, "SEMANTIC_METRICS_WEB_DISCOVERY", False
            ),
            provisional_overlays=_enabled(
                env, "SEMANTIC_METRICS_PROVISIONAL_OVERLAYS", False
            ),
            auto_publish_alias=_enabled(
                env, "SEMANTIC_METRICS_AUTO_PUBLISH_ALIAS", False
            ),
        )


__all__ = ["SemanticMetricFeatures"]
