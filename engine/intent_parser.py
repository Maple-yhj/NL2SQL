from datetime import datetime

from core.llm import LLMProtocol
from core.structured_output import extract_json_object
from engine.models import QueryIntent


INTENT_SYSTEM_TEMPLATE = """
You are a BI intent parser for a PostgreSQL NL2SQL engine.
Return only JSON. Do not explain.
Use the local system time below as the only reference for relative dates such as
today, yesterday, this week, this month, last month, recent N days, and year to date.
Local system datetime: {local_datetime}
Local system date: {local_date}
JSON shape:
{{
  "metrics": ["metric names such as gmv, paid_orders, refund_rate"],
  "time_range": {{"start": "YYYY-MM-DD or empty", "end": "YYYY-MM-DD or empty"}},
  "dimensions": ["dimension names such as region, category, paid_date"],
  "filters": ["business filters in concise natural language"]
}}
""".strip()


def build_intent_system(now: datetime | None = None) -> str:
    local_now = now or datetime.now().astimezone()
    return INTENT_SYSTEM_TEMPLATE.format(
        local_datetime=local_now.isoformat(),
        local_date=local_now.date().isoformat(),
    )


async def parse_intent(
    question: str,
    llm: LLMProtocol,
    now: datetime | None = None,
) -> QueryIntent:
    raw = await llm.complete(prompt=question, system=build_intent_system(now), max_output_tokens=1024)
    return QueryIntent.from_dict(extract_json_object(raw))
