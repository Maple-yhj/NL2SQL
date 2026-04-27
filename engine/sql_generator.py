import re

from catalog.loader import load_schema_snippet
from engine.executor import assert_readonly_sql, ensure_limit
from engine.metrics import MetricRegistry
from engine.models import QueryIntent


SQL_SYSTEM = """
You generate safe PostgreSQL SELECT SQL for a BI NL2SQL engine.
Rules:
- Return only one SQL statement.
- Use only tables and columns shown in the schema context.
- Generate read-only SELECT or WITH ... SELECT statements only.
- Prefer metric definitions from the metrics registry.
- Include GROUP BY for every non-aggregate selected dimension.
- Do not include markdown, explanations, DDL, DML, comments, or semicolon.
""".strip()


async def generate_sql(
    question: str,
    intent: QueryIntent,
    catalog: list[dict],
    metrics: MetricRegistry,
    llm,
) -> str:
    schema = load_schema_snippet(catalog, _tables_for_intent(intent))
    prompt = f"""
Question:
{question}

Parsed intent:
metrics: {intent.metrics}
time_range: {intent.time_range}
dimensions: {intent.dimensions}
filters: {intent.filters}

Metrics registry:
{metrics.prompt_block(intent.metrics)}

Schema catalog:
{schema}
""".strip()

    raw = await llm.complete(prompt=prompt, system=SQL_SYSTEM, max_output_tokens=2048)
    sql = ensure_limit(_extract_sql(raw))
    assert_readonly_sql(sql)
    return sql


def _extract_sql(text: str) -> str:
    value = text.strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1)
    value = value.strip().rstrip(";").strip()
    if not value:
        raise ValueError("Model returned empty SQL.")
    return re.sub(r"\s+", " ", value)


def _tables_for_intent(intent: QueryIntent) -> list[str]:
    names = set()
    for item in [*intent.metrics, *intent.dimensions, *intent.filters]:
        lower = item.lower()
        if any(word in lower for word in ["refund", "退款"]):
            names.add("refunds")
        if any(word in lower for word in ["product", "category", "商品", "品类"]):
            names.add("products")
        if any(word in lower for word in ["user", "用户", "复购"]):
            names.add("users")
        names.add("orders")
    return sorted(names) or ["orders"]
