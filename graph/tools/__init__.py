from importlib import import_module
from typing import Any


_EXPORTS = {
    "execute_sql": "graph.tools.execute_sql",
    "export_tool_manifest": "graph.tools.manifest_export",
    "export_tool_manifest_json": "graph.tools.manifest_export",
    "explain_result": "graph.tools.explain_result",
    "explain_table_result": "graph.tools.explain_table_result",
    "generate_sql": "graph.tools.sql_generator",
    "prepare_sql": "graph.tools.prepare_sql",
    "evaluate_pre_call_policy": "graph.tools.policy",
    "search_metrics": "graph.tools.sql_store",
    "search_schema": "graph.tools.sql_store",
    "summarize_tool_payload": "graph.tools.tracing",
    "validate_sql": "graph.tools.validate_sql",
    "RuntimeToolRegistry": "graph.tools.runtime_registry",
    "ToolRegistry": "graph.tools.registry",
    "ToolSpec": "graph.tools.contracts",
    "build_runtime_tool_registry": "graph.tools.runtime_registry",
    "default_tool_registry": "graph.tools.registry",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = import_module(_EXPORTS[name])
    value = getattr(module, name)
    globals()[name] = value
    return value
