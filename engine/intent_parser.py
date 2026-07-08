from datetime import datetime
import re

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
  "filters": ["business filters in concise natural language"],
  "limit": positive integer or null
}}
Set "limit" only when the user explicitly asks for a result count, for example
top N, first N rows, return N records, 返回前 N 个, 返回 N 条, 取前 N 个,
最近 N 条, or 最高/最低/最多的 N 个. Do not use counts that are filter
thresholds, metric values, scores, dates, years, or rating values.
""".strip()


_NUMBER_TOKEN = r"(?P<limit>\d{1,5}|[零一二三四五六七八九十百千万两]{1,8})"
_RESULT_UNIT = r"(?:条|个|行|名|笔|项|组|类|城市|州|商品|订单|记录|明细|组合)"
_LIMIT_PATTERNS = (
    re.compile(rf"\b(?:top|limit)\s+{_NUMBER_TOKEN}\b", re.IGNORECASE),
    re.compile(
        rf"\breturn\s+(?:the\s+)?(?:top\s+)?{_NUMBER_TOKEN}\s+"
        r"(?:rows?|records?|items?|cities|states|products|orders?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:first|latest)\s+{_NUMBER_TOKEN}\s+"
        r"(?:rows?|records?|items?|products|orders?)\b",
        re.IGNORECASE,
    ),
    re.compile(rf"(?:返回|取|列出|显示|展示|给出|查看|查询|找出)\s*(?:排名|排行)?\s*前\s*{_NUMBER_TOKEN}\s*{_RESULT_UNIT}?"),
    re.compile(rf"(?:返回|取|列出|显示|展示|给出|查看|查询|找出)\s*{_NUMBER_TOKEN}\s*{_RESULT_UNIT}"),
    re.compile(rf"(?:排名|排行)?\s*前\s*{_NUMBER_TOKEN}\s*{_RESULT_UNIT}"),
    re.compile(rf"(?:最近|最新|最早)\s*{_NUMBER_TOKEN}\s*{_RESULT_UNIT}"),
    re.compile(rf"(?:最高|最低|最大|最小|最多|最少|最佳|最差|增长最快)的?\s*{_NUMBER_TOKEN}\s*{_RESULT_UNIT}"),
)
_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


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
    intent = QueryIntent.from_dict(extract_json_object(raw))
    explicit_limit = _extract_explicit_limit(question)
    if explicit_limit is not None:
        intent.limit = explicit_limit
    return intent


def _extract_explicit_limit(question: str) -> int | None:
    for pattern in _LIMIT_PATTERNS:
        match = pattern.search(question)
        if match:
            return _parse_limit_token(match.group("limit"))
    return None


def _parse_limit_token(value: str) -> int | None:
    text = value.strip().replace(",", "")
    if text.isdigit():
        parsed = int(text)
        return parsed if parsed > 0 else None

    total = 0
    section = 0
    number = 0
    for char in text:
        if char in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[char]
            continue
        unit = _CHINESE_UNITS.get(char)
        if unit is None:
            return None
        if unit == 10000:
            section = (section + number) or 1
            total += section * unit
            section = 0
        else:
            section += (number or 1) * unit
        number = 0
    parsed = total + section + number
    return parsed if parsed > 0 else None
