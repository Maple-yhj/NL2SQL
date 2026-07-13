"""Closed registry for trusted, built-in, versioned skills."""

from __future__ import annotations

from types import MappingProxyType

from .commerce import CommerceAnalyticsSkill


class SkillRegistry:
    """Read-only built-in registry; dynamic plugin discovery is intentionally absent."""

    __slots__ = ("_skills",)

    def __init__(self) -> None:
        commerce = CommerceAnalyticsSkill()
        key = f"{commerce.manifest.skill_id}@{commerce.manifest.version}"
        self._skills = MappingProxyType({key: commerce})

    @classmethod
    def builtin(cls) -> "SkillRegistry":
        return BUILTIN_SKILL_REGISTRY

    def keys(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def skills(self) -> tuple[CommerceAnalyticsSkill, ...]:
        return tuple(self._skills.values())

    def get(
        self,
        skill_id: str,
        version: str | None = None,
    ) -> CommerceAnalyticsSkill | None:
        if "@" in skill_id:
            if version is not None:
                return None
            key = skill_id
        else:
            requested_version = version or "1.0.0"
            key = f"{skill_id}@{requested_version}"
        return self._skills.get(key)


BUILTIN_SKILL_REGISTRY = SkillRegistry()
