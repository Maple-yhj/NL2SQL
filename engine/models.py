from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class QueryIntent:
    metrics: list[str] = field(default_factory=list)
    time_range: dict[str, Any] = field(default_factory=dict)
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    limit: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryIntent":
        return cls(
            metrics=_string_list(data.get("metrics")),
            time_range=data.get("time_range") if isinstance(data.get("time_range"), dict) else {},
            dimensions=_string_list(data.get("dimensions")),
            filters=_string_list(data.get("filters")),
            limit=_intent_limit(data),
        )


@dataclass(slots=True)
class NL2SQLResult:
    question: str
    intent: QueryIntent
    sql: str
    rows: list[dict[str, Any]] = field(default_factory=list)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _intent_limit(data: dict[str, Any]) -> int | None:
    direct = coerce_positive_int(data.get("limit"))
    if direct is not None:
        return direct
    direct = coerce_positive_int(data.get("row_limit"))
    if direct is not None:
        return direct
    result_shape = data.get("result_shape")
    if isinstance(result_shape, dict):
        return coerce_positive_int(result_shape.get("limit"))
    return None


def coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.isdigit():
            parsed = int(text)
            return parsed if parsed > 0 else None
    return None
