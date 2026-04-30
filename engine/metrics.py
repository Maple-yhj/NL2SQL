from dataclasses import dataclass
from typing import Iterable


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
        return cls(
            [
                Metric(
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
                ),
                # Metric(
                #     id = 2,
                #     name="paid_orders",
                #     business_def="Number of paid orders.",
                #     sql_expr="count(*) filter (where orders.status = 'paid')",
                #     dimensions=("region", "paid_date", "product_id", "user_id"),
                #     synonyms=("paid order count", "订单数", "支付订单数"),
                # ),
                # Metric(
                #     id = 3,
                #     name="refund_rate",
                #     business_def="Refund amount divided by paid order amount.",
                #     sql_expr="coalesce(sum(refunds.amount), 0) / nullif(sum(orders.amount), 0)",
                #     dimensions=("region", "product_id", "user_id"),
                #     synonyms=("退款率", "refund ratio"),
                # ),
                # Metric(
                #     id = 4,
                #     name="repeat_purchase_rate",
                #     business_def="Share of users with at least two paid orders.",
                #     sql_expr=(
                #         "count(*) filter (where user_paid_orders >= 2)::numeric "
                #         "/ nullif(count(*), 0)"
                #     ),
                #     dimensions=("region", "user_type"),
                #     synonyms=("复购率", "repeat rate"),
                # ),
            ]
        )

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
