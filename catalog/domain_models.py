from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class DomainTable:
    name: str
    aliases: tuple[str, ...] = ()
    primary_keys: tuple[str, ...] = ()
    role: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "DomainTable":
        return cls(
            name=name,
            aliases=_string_tuple(data.get("aliases")),
            primary_keys=_string_tuple(data.get("primary_keys")),
            role=str(data.get("role") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DomainJoin:
    left: str
    right: str
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainJoin":
        return cls(
            left=str(data.get("left") or "").strip(),
            right=str(data.get("right") or "").strip(),
            description=str(data.get("description") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DomainTerm:
    name: str
    aliases: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    required_tables: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "DomainTerm":
        return cls(
            name=name,
            aliases=_string_tuple(data.get("aliases")),
            columns=_string_tuple(data.get("columns")),
            required_tables=_string_tuple(data.get("required_tables")),
            description=str(data.get("description") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DomainMetric:
    name: str
    expression: str
    base_table: str
    time_column: str = ""
    aliases: tuple[str, ...] = ()
    required_tables: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "DomainMetric":
        return cls(
            name=name,
            expression=str(data.get("expression") or "").strip(),
            base_table=str(data.get("base_table") or "").strip(),
            time_column=str(data.get("time_column") or "").strip(),
            aliases=_string_tuple(data.get("aliases")),
            required_tables=_string_tuple(data.get("required_tables")),
            description=str(data.get("description") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DomainCalculatedField:
    name: str
    expression: str
    aliases: tuple[str, ...] = ()
    required_tables: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "DomainCalculatedField":
        return cls(
            name=name,
            expression=str(data.get("expression") or "").strip(),
            aliases=_string_tuple(data.get("aliases")),
            required_tables=_string_tuple(data.get("required_tables")),
            description=str(data.get("description") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DomainQueryRule:
    name: str
    aliases: tuple[str, ...] = ()
    match_all: tuple[str, ...] = ()
    match_none: tuple[str, ...] = ()
    required_tables: tuple[str, ...] = ()
    optional_tables: tuple[str, ...] = ()
    suppressed_required_tables: tuple[str, ...] = ()
    required_columns: tuple[str, ...] = ()
    required_filters: tuple[str, ...] = ()
    required_group_by: tuple[str, ...] = ()
    required_order_by: tuple[str, ...] = ()
    required_sql_fragments: tuple[str, ...] = ()
    forbidden_sql_fragments: tuple[str, ...] = ()
    forbidden_tables: tuple[str, ...] = ()
    default_columns: tuple[str, ...] = ()
    sql_hints: tuple[str, ...] = ()
    forbid_select_star: bool = False
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "DomainQueryRule":
        return cls(
            name=name,
            aliases=_string_tuple(data.get("aliases")),
            match_all=_string_tuple(data.get("match_all")),
            match_none=_string_tuple(data.get("match_none")),
            required_tables=_string_tuple(data.get("required_tables")),
            optional_tables=_string_tuple(data.get("optional_tables")),
            suppressed_required_tables=_string_tuple(data.get("suppressed_required_tables")),
            required_columns=_string_tuple(data.get("required_columns")),
            required_filters=_string_tuple(data.get("required_filters")),
            required_group_by=_string_tuple(data.get("required_group_by")),
            required_order_by=_string_tuple(data.get("required_order_by")),
            required_sql_fragments=_string_tuple(data.get("required_sql_fragments")),
            forbidden_sql_fragments=_string_tuple(data.get("forbidden_sql_fragments")),
            forbidden_tables=_string_tuple(data.get("forbidden_tables")),
            default_columns=_string_tuple(data.get("default_columns")),
            sql_hints=_string_tuple(data.get("sql_hints")),
            forbid_select_star=bool(data.get("forbid_select_star")),
            description=str(data.get("description") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DomainTenantScope:
    mode: str = ""
    tenant_column: str = ""
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DomainTenantScope":
        value = data or {}
        return cls(
            mode=str(value.get("mode") or "").strip(),
            tenant_column=str(value.get("tenant_column") or "").strip(),
            description=str(value.get("description") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DomainProfile:
    domain_id: str
    display_name: str
    version: str
    tables: dict[str, DomainTable] = field(default_factory=dict)
    joins: tuple[DomainJoin, ...] = ()
    terms: dict[str, DomainTerm] = field(default_factory=dict)
    metrics: dict[str, DomainMetric] = field(default_factory=dict)
    calculated_fields: dict[str, DomainCalculatedField] = field(default_factory=dict)
    query_rules: dict[str, DomainQueryRule] = field(default_factory=dict)
    tenant_scope: DomainTenantScope = field(default_factory=DomainTenantScope)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainProfile":
        domain_id = str(data.get("domain_id") or "").strip()
        if not domain_id:
            raise ValueError("Domain profile requires domain_id.")
        display_name = str(data.get("display_name") or domain_id).strip()
        tables = data.get("tables") if isinstance(data.get("tables"), dict) else {}
        terms = data.get("terms") if isinstance(data.get("terms"), dict) else {}
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        calculated = (
            data.get("calculated_fields")
            if isinstance(data.get("calculated_fields"), dict)
            else {}
        )
        query_rules = (
            data.get("query_rules")
            if isinstance(data.get("query_rules"), dict)
            else {}
        )
        joins = data.get("joins") if isinstance(data.get("joins"), list) else []
        return cls(
            domain_id=domain_id,
            display_name=display_name,
            version=str(data.get("version") or "1"),
            tables={
                str(name): DomainTable.from_dict(str(name), value)
                for name, value in tables.items()
                if isinstance(value, dict)
            },
            joins=tuple(
                join
                for join in (DomainJoin.from_dict(value) for value in joins if isinstance(value, dict))
                if join.left and join.right
            ),
            terms={
                str(name): DomainTerm.from_dict(str(name), value)
                for name, value in terms.items()
                if isinstance(value, dict)
            },
            metrics={
                str(name): DomainMetric.from_dict(str(name), value)
                for name, value in metrics.items()
                if isinstance(value, dict)
            },
            calculated_fields={
                str(name): DomainCalculatedField.from_dict(str(name), value)
                for name, value in calculated.items()
                if isinstance(value, dict)
            },
            query_rules={
                str(name): DomainQueryRule.from_dict(str(name), value)
                for name, value in query_rules.items()
                if isinstance(value, dict)
            },
            tenant_scope=DomainTenantScope.from_dict(
                data.get("tenant_scope") if isinstance(data.get("tenant_scope"), dict) else None
            ),
        )


@dataclass(frozen=True, slots=True)
class DomainResolution:
    domain_id: str
    display_name: str
    required_tables: list[str] = field(default_factory=list)
    optional_tables: list[str] = field(default_factory=list)
    required_columns: list[str] = field(default_factory=list)
    join_hints: list[str] = field(default_factory=list)
    metric_hints: list[str] = field(default_factory=list)
    calculation_hints: list[str] = field(default_factory=list)
    tenant_scope_hints: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)
    required_filters: list[str] = field(default_factory=list)
    default_columns: list[str] = field(default_factory=list)
    sql_hints: list[str] = field(default_factory=list)
    required_group_by: list[str] = field(default_factory=list)
    required_order_by: list[str] = field(default_factory=list)
    required_sql_fragments: list[str] = field(default_factory=list)
    forbidden_sql_fragments: list[str] = field(default_factory=list)
    forbidden_tables: list[str] = field(default_factory=list)
    forbid_select_star: bool = False


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if item is not None)
    return (str(value),)
