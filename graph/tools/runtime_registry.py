from __future__ import annotations

from collections.abc import Mapping

from graph.tools.contracts import ToolHandler
from graph.tools.registry import ToolRegistry, ToolSpec, default_tool_registry


class RuntimeToolRegistry(ToolRegistry):
    """A tool registry with runtime handlers bound to manifest specs."""


def build_runtime_tool_registry(
    handlers: Mapping[str, ToolHandler] | None = None,
    *,
    manifest: ToolRegistry | None = None,
) -> RuntimeToolRegistry:
    manifest_registry = manifest or default_tool_registry()
    bound_handlers = dict(handlers or {})
    declared = set(manifest_registry.names())
    undeclared = sorted(set(bound_handlers) - declared)
    if undeclared:
        raise ValueError("Runtime handler tool is not declared: " + ", ".join(undeclared))

    registry = RuntimeToolRegistry()
    for name in manifest_registry.names():
        spec = manifest_registry.get(name)
        handler = bound_handlers.get(name, spec.handler)
        registry.register(_bind_handler(spec, handler))
    return registry


def _bind_handler(spec: ToolSpec, handler: ToolHandler) -> ToolSpec:
    return spec.model_copy(update={"handler": handler})
