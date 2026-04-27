from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class QueryIntent:
    metrics: list[str] = field(default_factory=list)
    time_range: dict[str, Any] = field(default_factory=dict)
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryIntent":
        return cls(
            metrics=_string_list(data.get("metrics")),
            time_range=data.get("time_range") if isinstance(data.get("time_range"), dict) else {},
            dimensions=_string_list(data.get("dimensions")),
            filters=_string_list(data.get("filters")),
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
