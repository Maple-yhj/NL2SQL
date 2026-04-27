from core.structured_output import extract_json_object
from engine.models import QueryIntent


INTENT_SYSTEM = """
You are a BI intent parser for a PostgreSQL NL2SQL engine.
Return only JSON. Do not explain.
JSON shape:
{
  "metrics": ["metric names such as gmv, paid_orders, refund_rate"],
  "time_range": {"start": "YYYY-MM-DD or empty", "end": "YYYY-MM-DD or empty"},
  "dimensions": ["dimension names such as region, category, paid_date"],
  "filters": ["business filters in concise natural language"]
}
""".strip()


async def parse_intent(question: str, llm) -> QueryIntent:
    raw = await llm.complete(prompt=question, system=INTENT_SYSTEM, max_output_tokens=1024)
    return QueryIntent.from_dict(extract_json_object(raw))
