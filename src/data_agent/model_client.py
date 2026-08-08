"""Dependency-light protocol shared by model-backed product components."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ModelClient(Protocol):
    model_id: str
    version: str

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str: ...


__all__ = ["ModelClient"]
