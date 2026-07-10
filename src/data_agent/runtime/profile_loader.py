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


def load_pack_yaml(path: str | Path, model: type[PackType]) -> PackType:
    """Load one YAML document without constructing executable Python objects."""

    try:
        raw_document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw_document, dict):
            raise TypeError("pack document root must be a mapping")
        return model.model_validate(raw_document)
    except (OSError, TypeError, ValidationError, yaml.YAMLError) as exc:
        raise PackLoadError(f"could not load a valid {model.__name__}") from exc


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
