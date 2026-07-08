from __future__ import annotations

import re
from typing import Any


_STRONG_DETAIL_TERMS = (
    "\u8bb0\u5f55",
    "\u660e\u7ec6",
    "\u6e05\u5355",
    "\u8be6\u60c5",
    "\u6240\u6709\u8ba2\u5355",
    "\u6bcf\u4e00\u7b14",
    "\u9010\u6761",
    "\u5bfc\u51fa",
    "\u67e5\u770b\u8ba2\u5355\u53f7",
    "record",
    "records",
    "detail",
    "details",
    "all orders",
    "order records",
)

_GENERIC_LIST_TERMS = (
    "\u5217\u8868",
    "\u5217\u51fa",
    "list",
)

_DOMAIN_DETAIL_TERMS = (
    "\u652f\u4ed8\u8bb0\u5f55",
    "\u652f\u4ed8\u660e\u7ec6",
    "\u5546\u54c1 id",
    "\u5546\u54c1id",
    "\u957f\u5bbd\u9ad8",
    "\u91cd\u91cf",
    "payment record",
    "payment detail",
    "product_id",
)

_SUMMARY_TERMS = (
    "\u591a\u5c11",
    "\u603b\u8ba1",
    "\u5408\u8ba1",
    "\u6c47\u603b",
    "\u5e73\u5747",
    "\u6700\u5927",
    "\u6700\u5c0f",
    "\u5bf9\u6bd4",
    "\u8d8b\u52bf",
    "\u5360\u6bd4",
    "\u6392\u540d",
    "\u8868\u73b0",
    "\u7ed3\u8bba",
    "how many",
    "total",
    "sum",
    "average",
    "avg",
    "compare",
    "trend",
    "ratio",
    "rank",
    "ranking",
    "gmv",
)

_AGGREGATE_SQL_RE = re.compile(r"\b(sum|count|avg|min|max)\s*\(|\bgroup\s+by\b", re.IGNORECASE)
_ORDER_BY_SQL_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)
_LIMIT_SQL_RE = re.compile(r"\blimit\s+\d+\b", re.IGNORECASE)
_TOPN_LIST_TERMS = (
    "\u524d",
    "\u6392\u540d",
    "\u6392\u884c",
    "\u6700\u591a",
    "\u6700\u5c11",
    "\u6700\u5927",
    "\u6700\u5c0f",
    "\u54ea\u4e9b",
    "top",
    "rank",
    "ranking",
    "most",
    "least",
    "highest",
    "lowest",
    "largest",
    "smallest",
)
_DETAIL_COLUMN_RE = re.compile(
    r"\b("
    r"order_id|customer_id|created_at|updated_at|amount|status|"
    r"product_id|product_category_name|product_length_cm|product_width_cm|"
    r"product_height_cm|product_weight_g|payment_type|payment_value|"
    r"payment_installments|payment_sequential|review_id|review_comment_message"
    r")\b",
    re.IGNORECASE,
)


def classify_message_type(
    *,
    question: str,
    contextualized_question: str = "",
    sql: str = "",
    rows: list[dict[str, Any]] | None = None,
    error: str = "",
) -> str:
    if error:
        return "error"

    row_count = len(rows or [])
    if row_count == 0:
        return "text"

    prompt = f"{question} {contextualized_question}".lower()
    if _contains_any(prompt, _STRONG_DETAIL_TERMS):
        return "table"
    if _contains_any(prompt, _DOMAIN_DETAIL_TERMS):
        return "table"
    if _looks_like_ranked_result_list(prompt=prompt, sql=sql, row_count=row_count):
        return "table"
    if _contains_any(prompt, _SUMMARY_TERMS):
        return "text"

    if _AGGREGATE_SQL_RE.search(sql):
        return "text"
    if _looks_like_detail_sql(sql):
        return "table"
    if _contains_any(prompt, _GENERIC_LIST_TERMS):
        return "table"

    return "text"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)


def _looks_like_detail_sql(sql: str) -> bool:
    if _AGGREGATE_SQL_RE.search(sql):
        return False
    return bool(_DETAIL_COLUMN_RE.search(sql))


def _looks_like_ranked_result_list(*, prompt: str, sql: str, row_count: int) -> bool:
    if row_count <= 1:
        return False
    if not (_ORDER_BY_SQL_RE.search(sql) and _LIMIT_SQL_RE.search(sql)):
        return False
    return _contains_any(prompt, _TOPN_LIST_TERMS)
