"""Safe YAML loading and deterministic JSON Schema export for packs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from .packs import DeploymentProfile, DomainPack, EnterpriseDataBinding


PackType = TypeVar("PackType", bound=BaseModel)


class PackLoadError(ValueError):
    """Raised when a pack document cannot be safely parsed and validated."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys at every depth."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


PACK_SCHEMA_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("domain-pack.schema.json", DomainPack),
    ("enterprise-binding.schema.json", EnterpriseDataBinding),
    ("deployment-profile.schema.json", DeploymentProfile),
)

_DOMAIN_PACK_FRAGMENTS: tuple[tuple[str, frozenset[str]], ...] = (
    ("semantic-model.yaml", frozenset({"entities", "relationships"})),
    ("metrics.yaml", frozenset({"metrics"})),
    ("vocabulary.zh-CN.yaml", frozenset({"vocabulary"})),
    ("policies.yaml", frozenset({"policies"})),
    ("evals.yaml", frozenset({"evals"})),
)


def _load_yaml_mapping(path: Path) -> dict:
    raw_document = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=_UniqueKeySafeLoader,
    )
    if not isinstance(raw_document, dict):
        raise TypeError("pack document root must be a mapping")
    return raw_document


def load_pack_yaml(path: str | Path, model: type[PackType]) -> PackType:
    """Load one YAML document without constructing executable Python objects."""

    try:
        raw_document = _load_yaml_mapping(Path(path))
        return model.model_validate(raw_document)
    except (OSError, TypeError, ValidationError, yaml.YAMLError) as exc:
        raise PackLoadError(f"could not load a valid {model.__name__}") from exc


def load_domain_pack(root: str | Path) -> DomainPack:
    """Load a split Domain Pack from a fixed, deterministic fragment set."""

    pack_root = Path(root)
    try:
        document = _load_yaml_mapping(pack_root / "pack.yaml")
        if "spec" in document:
            raise TypeError("split domain pack metadata must not contain spec")

        spec: dict = {}
        for filename, allowed_keys in _DOMAIN_PACK_FRAGMENTS:
            fragment = _load_yaml_mapping(pack_root / filename)
            unexpected = set(fragment) - allowed_keys
            missing = allowed_keys - set(fragment)
            if unexpected or missing:
                raise TypeError(f"invalid keys in domain pack fragment {filename}")
            overlap = set(spec) & set(fragment)
            if overlap:
                raise TypeError("domain pack fragments define duplicate sections")
            spec.update(fragment)

        document["spec"] = spec
        return DomainPack.model_validate(document)
    except (OSError, TypeError, ValidationError, yaml.YAMLError) as exc:
        raise PackLoadError("could not load a valid DomainPack") from exc


def load_enterprise_binding(root: str | Path) -> EnterpriseDataBinding:
    """Load an EnterpriseDataBinding from a monolithic or split pack.

    A monolithic ``pack.yaml`` is the canonical local representation.  For
    deployments that use the architecture's split layout, ``sources.yaml``,
    ``bindings/commerce.yaml`` and ``policies.yaml`` are merged using the same
    duplicate-key-safe loader before Pydantic validation.
    """

    pack_root = Path(root)
    path = pack_root if pack_root.is_file() else pack_root / "pack.yaml"
    try:
        document = _load_yaml_mapping(path)
        if "spec" in document:
            return EnterpriseDataBinding.model_validate(document)

        spec: dict = {}
        fragments: tuple[tuple[Path, frozenset[str]], ...] = (
            (pack_root / "sources.yaml", frozenset({"sources"})),
            (pack_root / "bindings" / "commerce.yaml", frozenset({"bindings", "relationships"})),
            (pack_root / "policies.yaml", frozenset({"policies"})),
        )
        for fragment_path, allowed_keys in fragments:
            fragment = _load_yaml_mapping(fragment_path)
            unexpected = set(fragment) - allowed_keys
            if unexpected:
                raise TypeError(f"invalid keys in enterprise binding fragment {fragment_path.name}")
            overlap = set(spec) & set(fragment)
            if overlap:
                raise TypeError("enterprise binding fragments define duplicate sections")
            spec.update(fragment)
        document["spec"] = spec
        return EnterpriseDataBinding.model_validate(document)
    except (OSError, TypeError, ValidationError, yaml.YAMLError) as exc:
        raise PackLoadError("could not load a valid EnterpriseDataBinding") from exc


def compile_profile_bundle(
    domain_root: str | Path,
    enterprise_binding: EnterpriseDataBinding,
    deployment_profile: DeploymentProfile,
    schema_catalog: str | Path | list[dict] | None = None,
):
    """Load a domain and compile a binding/profile pair for contract checks."""

    from .composition import compile_runtime_bundle

    return compile_runtime_bundle(
        load_domain_pack(domain_root),
        enterprise_binding,
        deployment_profile,
        runtime_version="1.0.0",
        skill_versions={"commerce.analytics": "1.0.0"},
        tool_registry_version="1.0.0",
        schema_catalog=schema_catalog,
    )


def _schema_bytes(model: type[BaseModel]) -> bytes:
    schema = model.model_json_schema(by_alias=True, mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    _close_pattern_mappings(schema)
    return (
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _close_pattern_mappings(value: object) -> None:
    if isinstance(value, dict):
        pattern_properties = value.get("patternProperties")
        if isinstance(pattern_properties, dict) and pattern_properties:
            patterns = [{"pattern": pattern} for pattern in pattern_properties]
            value["propertyNames"] = (
                patterns[0] if len(patterns) == 1 else {"anyOf": patterns}
            )
            value["additionalProperties"] = False
        for nested in tuple(value.values()):
            _close_pattern_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            _close_pattern_mappings(nested)


def export_pack_schemas(output_dir: str | Path) -> tuple[Path, ...]:
    """Export the three versioned pack schemas with byte-stable formatting."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []
    for filename, model in PACK_SCHEMA_MODELS:
        output_path = destination / filename
        content = _schema_bytes(model)
        if not output_path.exists() or output_path.read_bytes() != content:
            output_path.write_bytes(content)
        exported.append(output_path)
    return tuple(exported)
