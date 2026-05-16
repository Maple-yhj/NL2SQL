from importlib import import_module

from .descriptions import TOOL_DESCRIPTIONS, get_tool_description
from .validate_sql import validate_sql
from .sql_store import search_metrics,search_schema

__all__ = [
    "TOOL_DESCRIPTIONS",
    "get_tool_description",
    "search_metrics",
    "search_schema",
    "validate_sql"
]


def __getattr__(name: str):
    if name in {"search_metrics", "search_schema"}:
        sql_store = import_module(".sql_store", __name__)
        return getattr(sql_store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
