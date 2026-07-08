from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from graph.tools.registry import ToolRegistry


def export_tool_manifest(registry: ToolRegistry) -> dict[str, Any]:
    return {
        "version": "1",
        "tools": [_export_tool(registry.get(name)) for name in sorted(registry.names())],
    }


def export_tool_manifest_json(registry: ToolRegistry) -> str:
    return json.dumps(
        export_tool_manifest(registry),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _export_tool(spec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "aliases": list(spec.aliases),
        "description": spec.description,
        "input_keys": list(spec.input_keys),
        "output_keys": list(spec.output_keys),
        "input_schema": _schema_for(spec.input_schema),
        "output_schema": _schema_for(spec.output_schema),
        "requires_llm": spec.requires_llm,
        "requires_embeddings": spec.requires_embeddings,
        "requires_db": spec.requires_db,
        "risk_level": spec.risk_level,
        "side_effects": spec.side_effects,
        "retry_policy": spec.retry_policy.model_dump(),
        "response_formats": list(spec.response_formats),
        "examples": [example.model_dump() for example in spec.examples],
        "eval_tags": list(spec.eval_tags),
    }


def _schema_for(schema: type[BaseModel] | None) -> dict[str, Any]:
    if schema is None:
        return {}
    return schema.model_json_schema()
