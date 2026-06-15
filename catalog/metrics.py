from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Metric:
    id: int
    tenant_id: str
    name: str
    display_name: str
    business_def: str
    sql_expr: str
    base_table: str
    time_column: str
    active: bool
    dimensions: tuple[str, ...]
    join_tables: tuple[str, ...]
    filters: tuple[str, ...]
    forbidden: tuple[str, ...]
    synonyms: tuple[str, ...] = ()


DEMO_METRIC = Metric(
    id=1,
    tenant_id="demo",
    name="gmv",
    display_name="GMV",
    business_def="Paid gross merchandise value.",
    sql_expr="sum(orders.amount)",
    base_table="orders",
    time_column="paid_at",
    active=True,
    dimensions=("region", "paid_date", "product_id", "user_id"),
    join_tables=(),
    filters=("status IN ('paid', 'refunded')",),
    forbidden=(),
    synonyms=("sales", "transaction value"),
)


class MetricRegistry:
    def __init__(self, metrics: Iterable[Metric]):
        self._metrics = {metric.name.lower(): metric for metric in metrics}

    @classmethod
    def default(cls) -> "MetricRegistry":
        try:
            from db.database_manager import execute_query

            rows = execute_query("SELECT * FROM metrics_registry WHERE is_active = true")
        except Exception as exc:
            print(
                f"[WARN] Failed to load metrics_registry: {exc}. Using demo metrics.",
                file=sys.stderr,
            )
            rows = []

        metrics = [
            Metric(
                id=row["id"],
                tenant_id=row["tenant_id"],
                name=row["metric_name"],
                display_name=row["display_name"],
                business_def=row["business_def"],
                sql_expr=row["sql_expr"],
                base_table=row["base_table"],
                time_column=row["time_column"],
                active=row["is_active"],
                dimensions=tuple(row.get("dimensions") or ()),
                join_tables=tuple(row.get("join_tables") or ()),
                filters=tuple(row.get("filters") or ()),
                forbidden=tuple(row.get("forbidden") or ()),
                synonyms=tuple(row.get("synonyms") or ()),
            )
            for row in rows or []
        ]
        return cls(metrics or [DEMO_METRIC])

    def get(self, name: str) -> Metric | None:
        key = name.lower()
        direct = self._metrics.get(key)
        if direct:
            return direct
        for metric in self._metrics.values():
            if key in {synonym.lower() for synonym in metric.synonyms}:
                return metric
        return None

    def select(self, names: Iterable[str]) -> list[Metric]:
        selected = [metric for name in names if (metric := self.get(name))]
        return selected or list(self._metrics.values())
