"""Schema-catalog validation for enterprise bindings.

The catalog is data only: it is never interpreted as SQL or executable code.  A
binding is publishable only when every physical relation and column exists in
the catalog and its declared type is compatible with the canonical field type.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .packs import DomainPack, EnterpriseDataBinding


_CATALOG_TYPE_ALIASES: dict[str, str] = {
    "text": "string",
    "character varying": "string",
    "varchar": "string",
    "char": "string",
    "integer": "integer",
    "int": "integer",
    "int2": "integer",
    "int4": "integer",
    "int8": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "numeric": "decimal",
    "decimal": "decimal",
    "real": "decimal",
    "double precision": "decimal",
    "date": "date",
    "timestamp": "datetime",
    "timestamp without time zone": "datetime",
    "timestamp with time zone": "datetime",
    "boolean": "boolean",
    "bool": "boolean",
    "json": "json",
    "jsonb": "json",
}
_SAFE_CASTS: set[tuple[str, str]] = {
    ("integer", "string"),
    ("integer", "decimal"),
    ("decimal", "string"),
    ("decimal", "integer"),
    ("date", "datetime"),
    ("datetime", "date"),
}


def load_schema_catalog(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSON schema catalog and reject malformed entries."""

    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not load schema catalog {source}") from exc
    if not isinstance(value, list):
        raise ValueError("schema catalog must be a list")
    for index, table in enumerate(value):
        if not isinstance(table, dict) or not isinstance(table.get("table"), str):
            raise ValueError(f"schema catalog entry {index} has no table name")
        columns = table.get("columns")
        if not isinstance(columns, list):
            raise ValueError(f"schema catalog table {table['table']!r} has no columns")
        seen: set[str] = set()
        for column in columns:
            if not isinstance(column, dict):
                raise ValueError(f"schema catalog table {table['table']!r} has invalid column")
            name = column.get("name")
            data_type = column.get("type")
            if not isinstance(name, str) or not isinstance(data_type, str):
                raise ValueError(f"schema catalog table {table['table']!r} has invalid column")
            if name in seen:
                raise ValueError(f"schema catalog table {table['table']!r} has duplicate column")
            seen.add(name)
    return value


def schema_fingerprint(catalog: Iterable[Mapping[str, Any]]) -> str:
    """Return a deterministic fingerprint for a catalog's complete contents."""

    import hashlib

    normalized = []
    for table in catalog:
        normalized.append(
            {
                **{key: value for key, value in table.items() if key != "columns"},
                "columns": sorted(
                    (dict(column) for column in table.get("columns", [])),
                    key=lambda column: str(column.get("name", "")),
                ),
            }
        )
    canonical = json.dumps(
        sorted(normalized, key=lambda table: str(table.get("table", ""))),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _catalog_index(catalog: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for table in catalog:
        name = str(table["table"])
        index[name] = {
            str(column["name"]): str(column["type"])
            for column in table.get("columns", [])
        }
    return index


def _canonical_type(data_type: str) -> str | None:
    return _CATALOG_TYPE_ALIASES.get(data_type.strip().lower())


def validate_enterprise_binding_schema(
    domain_pack: DomainPack,
    enterprise_binding: EnterpriseDataBinding,
    catalog: Iterable[Mapping[str, Any]],
) -> str:
    """Validate relations, columns, relationship keys and types.

    Returns the catalog fingerprint when validation succeeds.
    """

    catalog = list(catalog)
    index = _catalog_index(catalog)
    relation_allowlist = set(enterprise_binding.spec.policies.relation_allowlist)
    for entity_id, binding in enterprise_binding.spec.bindings.items():
        if relation_allowlist and binding.relation not in relation_allowlist:
            raise ValueError(f"binding {entity_id!r} relation is not allowed")
        try:
            schema, relation = binding.relation.split(".", 1)
        except ValueError as exc:  # defensive; pydantic validates this too
            raise ValueError(f"invalid relation {binding.relation!r}") from exc
        if schema != "public" or binding.relation not in {
            f"public.{name}" for name in index
        }:
            raise ValueError(f"binding {entity_id!r} references unknown relation")
        columns = index[relation]
        entity = domain_pack.spec.entities[entity_id]
        for field_name, field_binding in binding.fields.items():
            if field_binding.column not in columns:
                raise ValueError(
                    f"binding {entity_id!r} field {field_name!r} references unknown column"
                )
            catalog_type = _canonical_type(columns[field_binding.column])
            canonical_type = entity.fields[field_name].type
            target_type = field_binding.cast or canonical_type
            compatible = (
                catalog_type is not None
                and target_type == canonical_type
                and (
                    catalog_type == target_type
                    or (catalog_type, target_type) in _SAFE_CASTS
                )
            )
            if not compatible:
                raise ValueError(
                    f"binding {entity_id!r} field {field_name!r} has incompatible type"
                )

    # Relationship key columns must be present in the corresponding entity
    # bindings and match the canonical relationship endpoints.
    relationships = {item.name: item for item in domain_pack.spec.relationships}
    configured = enterprise_binding.spec.relationships
    for name, relation in configured.items():
        canonical = relationships.get(name)
        if canonical is None:
            raise ValueError(f"binding relationship {name!r} is not declared by domain")
        if relation.from_entity != canonical.from_entity or relation.to_entity != canonical.to_entity:
            raise ValueError(f"binding relationship {name!r} endpoints do not match domain")
        from_binding = enterprise_binding.spec.bindings.get(relation.from_entity)
        to_binding = enterprise_binding.spec.bindings.get(relation.to_entity)
        if from_binding is None or to_binding is None:
            raise ValueError(f"binding relationship {name!r} has no entity mapping")
        if len(relation.from_columns) != len(canonical.from_fields):
            raise ValueError(f"binding relationship {name!r} key arity does not match domain")
        from_table = index[from_binding.relation.split(".", 1)[1]]
        to_table = index[to_binding.relation.split(".", 1)[1]]
        if set(relation.from_columns) - set(from_table) or set(relation.to_columns) - set(to_table):
            raise ValueError(f"binding relationship {name!r} references unknown key column")

    return schema_fingerprint(catalog)


__all__ = [
    "load_schema_catalog",
    "schema_fingerprint",
    "validate_enterprise_binding_schema",
]
