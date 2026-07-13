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
    ("date", "datetime"),
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
    table_names: set[str] = set()
    for index, table in enumerate(value):
        if not isinstance(table, dict) or not isinstance(table.get("table"), str):
            raise ValueError(f"schema catalog entry {index} has no table name")
        if table["table"] in table_names:
            raise ValueError(f"schema catalog has duplicate relation {table['table']!r}")
        table_names.add(table["table"])
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
        unique_keys = table.get("unique_keys")
        if not isinstance(unique_keys, list) or not unique_keys:
            raise ValueError(
                f"schema catalog table {table['table']!r} has no declared unique key"
            )
        for key in unique_keys:
            if (
                not isinstance(key, list)
                or not key
                or not all(isinstance(column, str) for column in key)
                or len(key) != len(set(key))
                or set(key) - seen
            ):
                raise ValueError(
                    f"schema catalog table {table['table']!r} has invalid unique key"
                )
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


def _catalog_index(catalog: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for table in catalog:
        name = str(table["table"])
        if name in index:
            raise ValueError(f"schema catalog has duplicate relation {name!r}")
        index[name] = {
            str(column["name"]): {
                "type": str(column["type"]),
                "nullable": bool(column.get("nullable", True)),
            }
            for column in table.get("columns", [])
        }
    return index


def _canonical_type(data_type: str) -> str | None:
    return _CATALOG_TYPE_ALIASES.get(data_type.strip().lower())


def _constant_matches_type(value: object, canonical_type: str) -> bool:
    if canonical_type == "string":
        return isinstance(value, str)
    if canonical_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if canonical_type == "decimal":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if canonical_type == "boolean":
        return isinstance(value, bool)
    return False


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
    unique_keys_by_relation = {
        str(table["table"]): {
            tuple(str(column) for column in key)
            for key in table.get("unique_keys", [])
        }
        for table in catalog
    }
    relation_allowlist = set(enterprise_binding.spec.policies.relation_allowlist)
    known_relations = {f"public.{name}" for name in index}
    unknown_allowlist = relation_allowlist - known_relations
    if unknown_allowlist:
        raise ValueError(
            "relation allowlist references unknown relation(s): "
            + ", ".join(sorted(unknown_allowlist))
        )
    for entity_id, binding in enterprise_binding.spec.bindings.items():
        if relation_allowlist and binding.relation not in relation_allowlist:
            raise ValueError(f"binding {entity_id!r} relation is not allowed")
        try:
            schema, relation = binding.relation.split(".", 1)
        except ValueError as exc:  # defensive; pydantic validates this too
            raise ValueError(f"invalid relation {binding.relation!r}") from exc
        if schema != "public" or binding.relation not in known_relations:
            raise ValueError(f"binding {entity_id!r} references unknown relation")
        columns = index[relation]
        entity = domain_pack.spec.entities[entity_id]
        for field_name, field_binding in binding.fields.items():
            if field_binding.value is not None:
                if field_binding.column is not None:
                    raise ValueError(f"binding {entity_id!r} constant field has a physical column")
                if not _constant_matches_type(
                    field_binding.value, entity.fields[field_name].type
                ):
                    raise ValueError(
                        f"binding {entity_id!r} field {field_name!r} has incompatible constant type"
                    )
                continue
            if field_binding.column not in columns:
                raise ValueError(
                    f"binding {entity_id!r} field {field_name!r} references unknown column"
                )
            catalog_column = columns[field_binding.column]
            catalog_type = _canonical_type(catalog_column["type"])
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
            if field_binding.coalesce_value is not None and not _constant_matches_type(
                field_binding.coalesce_value, canonical_type
            ):
                raise ValueError(
                    f"binding {entity_id!r} field {field_name!r} has incompatible coalesce type"
                )
            if entity.fields[field_name].type == "datetime" and not field_binding.timezone:
                raise ValueError(
                    f"binding {entity_id!r} datetime field {field_name!r} requires timezone"
                )
            if (
                enterprise_binding.spec.policies.access_mode == "tenant_scoped"
                and not entity.fields[field_name].nullable
                and catalog_column["nullable"]
                and field_binding.null_policy == "preserve"
            ):
                raise ValueError(
                    f"binding {entity_id!r} non-nullable field {field_name!r} preserves catalog nulls"
                )

        grain_columns = []
        for grain_field in binding.grain:
            grain_binding = binding.fields[grain_field]
            if grain_binding.column is None:
                raise ValueError(f"binding {entity_id!r} grain field must map to a column")
            grain_columns.append(grain_binding.column)
        if len(grain_columns) != len(set(grain_columns)):
            raise ValueError(f"binding {entity_id!r} physical grain columns are not unique")
        if tuple(grain_columns) not in unique_keys_by_relation[relation]:
            raise ValueError(
                f"binding {entity_id!r} physical grain is not a declared unique key"
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
