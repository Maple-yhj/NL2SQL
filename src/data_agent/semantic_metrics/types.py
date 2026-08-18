"""Shared primitive types for the semantic metric package."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
StableIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$",
    ),
]


__all__ = ["NonBlankText", "StableIdentifier"]
