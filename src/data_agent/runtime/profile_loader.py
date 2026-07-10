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
    raw_document = yaml.safe_load(path.read_text(encoding="utf-8"))
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
