from dataclasses import dataclass
import sys
from typing import Iterable


def execute_query(sql: str):
    from db.database_manager import execute_query as db_execute_query

    return db_execute_query(sql)

@dataclass(frozen=True, slots=True)
class Metric:
    id: int
    tenant_id: str
    name: str
    display_name:str
    business_def: str
    sql_expr: str
    base_table: str
    time_column: str
    activate:bool
    dimensions: tuple[str, ...]
    join_tables: tuple[str, ...]
    filters: tuple[str, ...]
    forbidden : tuple[str, ...]
    synonyms: tuple[str, ...] = ()
    
    


class MetricRegistry:
    def __init__(self, metrics: Iterable[Metric]):
        self._metrics = {metric.name: metric for metric in metrics}

    @classmethod
    def default(cls) -> "MetricRegistry":
        try:
            rows = execute_query("SELECT * FROM metrics_registry WHERE is_active = true")
        except Exception as exc:  # noqa: BLE001
            print(
                f"[WARN] Failed to load metrics_registry from database: {exc}. "
                "Using built-in demo metrics.",
                file=sys.stderr,
            )
            rows = []

        active_metrics : list[Metric] = []
        if rows:
            for row in rows:
                active_metrics.append(Metric(
                    id = row["id"],
                    tenant_id = row["tenant_id"],
                    name = row["metric_name"],
                    display_name = row["display_name"],
                    business_def = row["business_def"],
                    sql_expr = row["sql_expr"],
                    base_table = row["base_table"],
                    time_column = row["time_column"],
                    activate = row["is_active"],
                    dimensions = row["dimensions"],
                    join_tables = row["join_tables"],
                    filters = row["filters"],
                    forbidden = row["forbidden"],
                    synonyms = row["synonyms"],
                ))
        else:
            active_metrics.append(Metric(
                    id = 1,
                    tenant_id = "demo",
                    name="gmv",
                    display_name = "GMV",
                    business_def="Paid gross merchandise value.",
                    sql_expr="sum(orders.amount)",
                    base_table = "orders",
                    time_column="",
                    activate = True,
                    dimensions=("region", "paid_date", "product_id", "user_id"),
                    join_tables=("LEFT JOIN refunds r ON r.order_id = o.id AND r.status = 'approved'",),
                    filters = ("status IN ('paid','refunded')","demo"),
                    forbidden = ("不得用于利润计算","利润需减去成本和退款"),
                    synonyms=("GMV", "sales", "销售额", "成交额"),
                ))
        return cls(active_metrics)


    def get(self, name: str) -> Metric | None:
        key = name.lower()
        if key in self._metrics:
            return self._metrics[key]
        for metric in self._metrics.values():
            if key in {synonym.lower() for synonym in metric.synonyms}:
                return metric
        return None

    def select(self, names: Iterable[str]) -> list[Metric]:
        selected = [metric for name in names if (metric := self.get(name))]
        return selected or list(self._metrics.values())

    def prompt_block(self, names: Iterable[str] | None = None) -> str:
        selected = self.select(names or [])
        lines: list[str] = []
        for metric in selected:
            lines.append(f"- {metric.name}: {metric.business_def}")
            lines.append(f"  sql_expr: {metric.sql_expr}")
            lines.append(f"  dimensions: {', '.join(metric.dimensions)}")
            if metric.synonyms:
                lines.append(f"  synonyms: {', '.join(metric.synonyms)}")
        return "\n".join(lines)
