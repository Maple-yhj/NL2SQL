"""Closed registry and skill-scoped views for governed tools."""

from __future__ import annotations

from types import MappingProxyType

from .models import ToolInvocationContext, ToolProvider, ToolSpec


class ToolRegistryView:
    __slots__ = ("_specs",)

    def __init__(self, specs: dict[str, ToolSpec]) -> None:
        self._specs = MappingProxyType(dict(specs))

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)


class ToolRegistry:
    """Versioned registry that rejects provider/manifest drift."""

    __slots__ = ("version", "_specs", "_providers", "_frozen")

    def __init__(self, *, version: str) -> None:
        self.version = version
        self._specs: dict[str, ToolSpec] = {}
        self._providers: dict[str, ToolProvider] = {}
        self._frozen = False

    def register(self, spec: ToolSpec, provider: ToolProvider) -> None:
        if self._frozen:
            raise TypeError("tool registry is frozen")
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        provider_spec = getattr(provider, "spec", None)
        if provider_spec != spec:
            raise ValueError("provider manifest does not match registered ToolSpec")
        self._specs[spec.name] = spec
        self._providers[spec.name] = provider

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def provider(self, name: str) -> ToolProvider | None:
        return self._providers.get(name)

    def allowed_view(self, context: ToolInvocationContext) -> ToolRegistryView:
        allowed = frozenset(context.allowed_tools)
        authority_kind = context.authority.kind
        mode = context.mode
        return ToolRegistryView(
            {
                name: spec
                for name, spec in self._specs.items()
                if name in allowed
                and set(spec.required_capabilities).issubset(allowed)
                and authority_kind in spec.authority_kinds
                and mode in spec.allowed_modes
            }
        )
