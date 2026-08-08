"""Bounded, value-free local profile summaries for relationship calibration."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from itertools import islice
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ProfileBudget:
    max_values_per_column: int = 10_000
    max_candidate_pairs: int = 500

    def __post_init__(self) -> None:
        if self.max_values_per_column < 1 or self.max_candidate_pairs < 1:
            raise ValueError("profile budgets must be positive")


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    row_count: int
    null_count: int
    distinct_count: int
    type_family: str
    minimum: int | float | str | None = None
    maximum: int | float | str | None = None
    normalized_name_tokens: tuple[str, ...] = ()

    @property
    def null_rate(self) -> float:
        return self.null_count / self.row_count if self.row_count else 0.0

    @property
    def distinct_rate(self) -> float:
        non_null = self.row_count - self.null_count
        return self.distinct_count / non_null if non_null else 0.0

    @property
    def unique(self) -> bool | None:
        non_null = self.row_count - self.null_count
        return self.distinct_count == non_null if non_null else None


@dataclass(frozen=True, slots=True)
class PairProfile:
    type_compatible: bool
    normalized_name_similarity: float
    match_rate: float | None
    left_orphan_rate: float | None
    right_orphan_rate: float | None
    left_unique: bool | None
    right_unique: bool | None
    average_fanout: float | None
    maximum_fanout: int | None
    estimated_joined_rows: int | None
    expansion_ratio: float | None


def profile_values(
    values: Iterable[object],
    *,
    name: str = "",
    budget: ProfileBudget = ProfileBudget(),
) -> ColumnProfile:
    """Produce a bounded summary and intentionally return no raw value sketch."""

    materialized = _sample(values, budget)
    non_null = tuple(value for value in materialized if value is not None)
    family = _family(non_null[0]) if non_null else "unknown"
    compatible_values = tuple(value for value in non_null if _family(value) == family)
    minimum, maximum = _bounds(compatible_values, family)
    return ColumnProfile(
        row_count=len(materialized),
        null_count=len(materialized) - len(non_null),
        distinct_count=len({_canonical(value) for value in non_null}),
        type_family=family,
        minimum=minimum,
        maximum=maximum,
        normalized_name_tokens=_name_tokens(name),
    )


def profile_pair(
    left_values: Iterable[object],
    right_values: Iterable[object],
    *,
    left_name: str = "",
    right_name: str = "",
    budget: ProfileBudget = ProfileBudget(),
) -> PairProfile:
    """Calibrate one candidate pair under a strict per-column value budget."""

    left = tuple(value for value in _sample(left_values, budget) if value is not None)
    right = tuple(value for value in _sample(right_values, budget) if value is not None)
    left_profile = profile_values(left, name=left_name, budget=budget)
    right_profile = profile_values(right, name=right_name, budget=budget)
    compatible = _families_compatible(left_profile.type_family, right_profile.type_family)
    similarity = _jaccard(left_profile.normalized_name_tokens, right_profile.normalized_name_tokens)
    if not compatible or not left or not right:
        return PairProfile(
            type_compatible=compatible,
            normalized_name_similarity=similarity,
            match_rate=None,
            left_orphan_rate=None,
            right_orphan_rate=None,
            left_unique=left_profile.unique,
            right_unique=right_profile.unique,
            average_fanout=None,
            maximum_fanout=None,
            estimated_joined_rows=None,
            expansion_ratio=None,
        )
    left_counts = Counter(_canonical(value) for value in left)
    right_counts = Counter(_canonical(value) for value in right)
    matched = set(left_counts) & set(right_counts)
    joined_rows = sum(left_counts[value] * right_counts[value] for value in matched)
    matched_left_rows = sum(left_counts[value] for value in matched)
    matched_right_rows = sum(right_counts[value] for value in matched)
    fanouts = [right_counts[value] for value in matched for _ in range(left_counts[value])]
    return PairProfile(
        type_compatible=True,
        normalized_name_similarity=similarity,
        match_rate=matched_left_rows / len(left),
        left_orphan_rate=1 - (matched_left_rows / len(left)),
        right_orphan_rate=1 - (matched_right_rows / len(right)),
        left_unique=left_profile.unique,
        right_unique=right_profile.unique,
        average_fanout=(sum(fanouts) / len(fanouts)) if fanouts else 0.0,
        maximum_fanout=max(fanouts, default=0),
        estimated_joined_rows=joined_rows,
        expansion_ratio=joined_rows / len(left) if left else None,
    )


def _family(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (date, datetime)):
        return "temporal"
    return "text"


def _sample(values: Iterable[object], budget: ProfileBudget) -> tuple[object, ...]:
    """A connector/sample failure is unknown evidence, never negative evidence."""

    try:
        return tuple(islice(values, budget.max_values_per_column))
    except Exception:
        return ()


def _bounds(values: tuple[object, ...], family: str) -> tuple[int | float | str | None, int | float | str | None]:
    if family not in {"number", "text", "temporal"} or not values:
        return None, None
    try:
        return min(values), max(values)  # type: ignore[return-value,arg-type]
    except TypeError:
        return None, None


def _canonical(value: object) -> str:
    return str(value).strip().casefold()


def _name_tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token in value.casefold().replace("_", " ").split() if token)


def _families_compatible(left: str, right: str) -> bool:
    return left == right and left != "unknown"


def _jaccard(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    if not left and not right:
        return 0.0
    return len(set(left) & set(right)) / len(set(left) | set(right))


__all__ = ["ColumnProfile", "PairProfile", "ProfileBudget", "profile_pair", "profile_values"]
