"""Strict, non-executable configuration contracts for Data Agent packs."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
_PACK_NAME_BODY = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
_SEMVER_PRERELEASE_ID = (
    r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
_SEMVER_BODY = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    rf"(?:-{_SEMVER_PRERELEASE_ID}(?:\.{_SEMVER_PRERELEASE_ID})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
PackName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=rf"^{_PACK_NAME_BODY}$"),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=rf"^{_SEMVER_BODY}$"),
]
PackReferenceText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^{_PACK_NAME_BODY}@{_SEMVER_BODY}$",
    ),
]
LocalFieldName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]*$"),
]
CanonicalEntityId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^{_PACK_NAME_BODY}\.[A-Z][A-Za-z0-9]*$",
    ),
]
CanonicalLogicalId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^{_PACK_NAME_BODY}\.[a-z][a-z0-9_]*$",
    ),
]
CanonicalFieldRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^{_PACK_NAME_BODY}\.[A-Z][A-Za-z0-9]*\.[a-z][a-z0-9_]*$",
    ),
]
CanonicalSemanticRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=(
            rf"^{_PACK_NAME_BODY}\."
            r"(?:[A-Z][A-Za-z0-9]*(?:\.[a-z][a-z0-9_]*)?|[a-z][a-z0-9_]*)$"
        ),
    ),
]
PostgresIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z_][a-z0-9_]*$"),
]
QualifiedPostgresRelation = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$",
    ),
]
SecretReference = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^secret://[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+$",
    ),
]
EnvironmentVariable = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9_]*$"),
]


_EXECUTABLE_KEYS = {
    "sql",
    "rawsql",
    "python",
    "jinja",
    "shell",
    "import",
    "importpath",
    "module",
    "handler",
    "callable",
    "script",
    "command",
}
_TEMPLATE_MARKERS = ("{{", "{%", "{#")


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _reject_executable_content(value: Any, path: str = "configuration") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalized_key(key) in _EXECUTABLE_KEYS:
                raise ValueError(f"{path} contains forbidden executable field {key!r}")
            _reject_executable_content(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_executable_content(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and any(marker in value for marker in _TEMPLATE_MARKERS):
        raise ValueError(f"{path} contains a forbidden template expression")


class PackModel(BaseModel):
    """Base model shared by every node in a pack document."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        validate_default=True,
    )

    @model_validator(mode="before")
    @classmethod
    def reject_executable_configuration(cls, value: Any) -> Any:
        _reject_executable_content(value)
        return value


class PackMetadata(PackModel):
    name: PackName
    version: SemanticVersion = "1.0.0"


class CanonicalField(PackModel):
    type: Literal[
        "string",
        "integer",
        "decimal",
        "boolean",
        "date",
        "datetime",
        "json",
    ]
    nullable: bool = True
    unit: NonBlankText | None = None
    description: NonBlankText | None = None
    time_semantics: NonBlankText | None = None


class CanonicalEntity(PackModel):
    grain: tuple[LocalFieldName, ...] = Field(min_length=1)
    fields: dict[LocalFieldName, CanonicalField] = Field(default_factory=dict)
    description: NonBlankText | None = None


class CanonicalRelationship(PackModel):
    name: CanonicalLogicalId
    from_entity: CanonicalEntityId
    to_entity: CanonicalEntityId
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    from_fields: tuple[LocalFieldName, ...] = Field(min_length=1)
    to_fields: tuple[LocalFieldName, ...] = Field(min_length=1)


class CanonicalMetric(PackModel):
    aggregation: Literal["sum", "count", "count_distinct", "average", "min", "max"]
    inputs: tuple[CanonicalFieldRef, ...] = Field(min_length=1)
    combine: Literal["identity", "add"] = "identity"
    event_time: CanonicalFieldRef | None = None
    description: NonBlankText | None = None


class VocabularyEntry(PackModel):
    term: NonBlankText
    refs: tuple[CanonicalSemanticRef, ...] = Field(min_length=1)
    locale: NonBlankText = "zh-CN"


class DomainPolicy(PackModel):
    name: CanonicalLogicalId
    description: NonBlankText


EvalScalar = NonBlankText | int | float | bool


class DomainEvalPredicate(PackModel):
    ref: CanonicalSemanticRef
    operator: Literal[
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "is_null",
        "is_not_null",
        "contains",
    ]
    value: EvalScalar | tuple[EvalScalar, ...] | None = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> "DomainEvalPredicate":
        if self.operator in {"is_null", "is_not_null"}:
            if self.value is not None:
                raise ValueError("null predicates must not define a value")
        elif self.value is None:
            raise ValueError("predicate requires a value")
        if self.operator in {"in", "not_in"} and not isinstance(self.value, tuple):
            raise ValueError("set predicates require a list value")
        return self


class DomainEvalTime(PackModel):
    field: CanonicalFieldRef
    grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    start: NonBlankText | None = None
    end: NonBlankText | None = None

    @model_validator(mode="after")
    def validate_time_expectation(self) -> "DomainEvalTime":
        if self.grain is None and self.start is None and self.end is None:
            raise ValueError("time expectation must define grain or range")
        return self


class DomainEvalCalculation(PackModel):
    id: CanonicalLogicalId
    operation: Literal[
        "sum",
        "average",
        "count",
        "count_distinct",
        "add",
        "subtract",
        "multiply",
        "growth",
        "lag",
        "date_difference",
    ]
    inputs: tuple[CanonicalSemanticRef, ...] = Field(min_length=1)
    partition_by: tuple[CanonicalFieldRef, ...] = ()


class DomainEvalOrdering(PackModel):
    ref: CanonicalSemanticRef
    direction: Literal["asc", "desc"]


class DomainEvalContext(PackModel):
    mode: Literal["standalone", "follow_up"] = "standalone"
    tenant_scope: Literal["all", "seller"] = "all"
    prior_question: NonBlankText | None = None
    preserve: tuple[
        Literal["metrics", "filters", "time_range", "tenant_scope", "grain"],
        ...,
    ] = ()


class DomainEvalCase(PackModel):
    id: CanonicalLogicalId
    question: NonBlankText
    analysis_type: Literal[
        "metric",
        "trend",
        "ranking",
        "detail",
        "comparison",
        "cross_tab",
        "distribution",
        "derived",
        "follow_up",
        "tenant_scoped",
    ] = "metric"
    expected_metrics: tuple[CanonicalLogicalId, ...] = ()
    expected_entities: tuple[CanonicalEntityId, ...] = ()
    expected_dimensions: tuple[CanonicalFieldRef, ...] = ()
    expected_fields: tuple[CanonicalFieldRef, ...] = ()
    filters: tuple[DomainEvalPredicate, ...] = ()
    time: DomainEvalTime | None = None
    calculations: tuple[DomainEvalCalculation, ...] = ()
    having: tuple[DomainEvalPredicate, ...] = ()
    ordering: tuple[DomainEvalOrdering, ...] = ()
    limit: int | None = Field(default=None, ge=1)
    expected_grain: tuple[CanonicalFieldRef, ...] = ()
    context: DomainEvalContext = Field(default_factory=DomainEvalContext)


class DomainPackSpec(PackModel):
    entities: dict[CanonicalEntityId, CanonicalEntity] = Field(default_factory=dict)
    relationships: tuple[CanonicalRelationship, ...] = ()
    metrics: dict[CanonicalLogicalId, CanonicalMetric] = Field(default_factory=dict)
    vocabulary: tuple[VocabularyEntry, ...] = ()
    policies: tuple[DomainPolicy, ...] = ()
    evals: tuple[DomainEvalCase, ...] = ()


class DomainPack(PackModel):
    api_version: Literal["dataagent.io/domain/v1"]
    kind: Literal["DomainPack"]
    metadata: PackMetadata
    spec: DomainPackSpec

    @model_validator(mode="before")
    @classmethod
    def reject_physical_identifiers(cls, value: Any) -> Any:
        forbidden = {"relation", "table", "column", "schema", "connector", "connectionref"}

        def walk(item: Any, path: str = "domain pack") -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if _normalized_key(key) in forbidden:
                        raise ValueError(
                            f"{path} contains forbidden physical field {key!r}"
                        )
                    walk(nested, f"{path}.{key}")
            elif isinstance(item, (list, tuple)):
                for index, nested in enumerate(item):
                    walk(nested, f"{path}[{index}]")

        walk(value)
        return value

    @model_validator(mode="after")
    def validate_logical_references(self) -> "DomainPack":
        domain_prefix = f"{self.metadata.name}."
        entities = self.spec.entities
        metrics = self.spec.metrics
        relationships = {item.name for item in self.spec.relationships}
        policies = {item.name for item in self.spec.policies}

        for entity_id, entity in entities.items():
            if not entity_id.startswith(domain_prefix):
                raise ValueError(f"entity {entity_id!r} is outside the domain pack")
            missing_grain = set(entity.grain) - set(entity.fields)
            if missing_grain:
                raise ValueError(f"entity {entity_id!r} grain references missing fields")

        declared_fields = {
            f"{entity_id}.{field_name}"
            for entity_id, entity in entities.items()
            for field_name in entity.fields
        }

        for relationship in self.spec.relationships:
            if not relationship.name.startswith(domain_prefix):
                raise ValueError("relationship is outside the domain pack")
            if relationship.from_entity not in entities:
                raise ValueError("relationship fromEntity is not declared")
            if relationship.to_entity not in entities:
                raise ValueError("relationship toEntity is not declared")
            if set(relationship.from_fields) - set(
                entities[relationship.from_entity].fields
            ):
                raise ValueError("relationship fromFields are not declared")
            if set(relationship.to_fields) - set(
                entities[relationship.to_entity].fields
            ):
                raise ValueError("relationship toFields are not declared")
            if len(relationship.from_fields) != len(relationship.to_fields):
                raise ValueError("relationship field arity does not match")

        for metric_id, metric in metrics.items():
            if not metric_id.startswith(domain_prefix):
                raise ValueError(f"metric {metric_id!r} is outside the domain pack")
            if set(metric.inputs) - declared_fields:
                raise ValueError(f"metric {metric_id!r} references missing fields")
            if metric.event_time is not None and metric.event_time not in declared_fields:
                raise ValueError(f"metric {metric_id!r} eventTime is not declared")

        allowed_vocabulary_refs = (
            set(entities) | declared_fields | set(metrics) | relationships | policies
        )
        for entry in self.spec.vocabulary:
            if set(entry.refs) - allowed_vocabulary_refs:
                raise ValueError(f"vocabulary term {entry.term!r} has missing refs")

        for policy in self.spec.policies:
            if not policy.name.startswith(domain_prefix):
                raise ValueError("policy is outside the domain pack")

        eval_ids: set[str] = set()
        for case in self.spec.evals:
            if not case.id.startswith(domain_prefix):
                raise ValueError("eval case is outside the domain pack")
            if case.id in eval_ids:
                raise ValueError(f"duplicate eval case {case.id!r}")
            eval_ids.add(case.id)
            if set(case.expected_metrics) - set(metrics):
                raise ValueError(f"eval case {case.id!r} has missing metrics")
            if set(case.expected_entities) - set(entities):
                raise ValueError(f"eval case {case.id!r} has missing entities")
            logical_fields = (
                set(case.expected_dimensions)
                | set(case.expected_fields)
                | set(case.expected_grain)
            )
            if logical_fields - declared_fields:
                raise ValueError(f"eval case {case.id!r} has missing fields")
            if case.time is not None and case.time.field not in declared_fields:
                raise ValueError(f"eval case {case.id!r} has missing time field")

            calculation_ids: set[str] = set()
            available_calculation_inputs = declared_fields | set(metrics)
            for calculation in case.calculations:
                if not calculation.id.startswith(domain_prefix):
                    raise ValueError("eval calculation is outside the domain pack")
                if calculation.id in calculation_ids:
                    raise ValueError("eval calculation IDs must be unique")
                if set(calculation.inputs) - available_calculation_inputs:
                    raise ValueError(
                        f"eval calculation {calculation.id!r} has missing inputs"
                    )
                if set(calculation.partition_by) - declared_fields:
                    raise ValueError(
                        f"eval calculation {calculation.id!r} has missing partitions"
                    )
                calculation_ids.add(calculation.id)
                available_calculation_inputs.add(calculation.id)

            allowed_eval_refs = declared_fields | set(metrics) | calculation_ids
            for predicate in (*case.filters, *case.having):
                if predicate.ref not in allowed_eval_refs:
                    raise ValueError(
                        f"eval case {case.id!r} predicate has missing ref"
                    )
            for ordering in case.ordering:
                if ordering.ref not in allowed_eval_refs:
                    raise ValueError(
                        f"eval case {case.id!r} ordering has missing ref"
                    )

        return self


class PackReference(PackModel):
    ref: PackReferenceText


class EnterpriseSource(PackModel):
    connector: Literal["postgres"]
    connection_ref: SecretReference
    read_only: Literal[True]


class PhysicalFieldBinding(PackModel):
    column: PostgresIdentifier
    cast: Literal["string", "integer", "decimal", "boolean", "date", "datetime"] | None = None
    timezone: NonBlankText | None = None
    null_policy: Literal["preserve", "reject", "coalesce"] = "preserve"
    enum_mapping: dict[NonBlankText, NonBlankText] = Field(default_factory=dict)


class PhysicalEntityBinding(PackModel):
    source: PostgresIdentifier
    relation: QualifiedPostgresRelation
    grain: tuple[LocalFieldName, ...] = Field(min_length=1)
    fields: dict[LocalFieldName, PhysicalFieldBinding] = Field(default_factory=dict)


class PhysicalRelationshipBinding(PackModel):
    """Explicit physical key columns for one canonical relationship."""

    from_entity: CanonicalEntityId
    to_entity: CanonicalEntityId
    from_columns: tuple[PostgresIdentifier, ...] = Field(min_length=1)
    to_columns: tuple[PostgresIdentifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_key_arity(self) -> "PhysicalRelationshipBinding":
        if len(self.from_columns) != len(self.to_columns):
            raise ValueError("physical relationship key arity does not match")
        return self


class TenantScopePolicy(PackModel):
    mode: NonBlankText
    canonical_field: CanonicalFieldRef
    principal_claim: LocalFieldName


class EnterprisePolicies(PackModel):
    tenant_scope: TenantScopePolicy | None = None
    max_rows: int = Field(default=1000, ge=1)
    query_timeout_seconds: int = Field(default=10, ge=1)
    relation_allowlist: tuple[QualifiedPostgresRelation, ...] = ()


class EnterpriseBindingSpec(PackModel):
    domains: tuple[PackReference, ...] = Field(min_length=1)
    sources: dict[PostgresIdentifier, EnterpriseSource] = Field(min_length=1)
    bindings: dict[CanonicalEntityId, PhysicalEntityBinding] = Field(default_factory=dict)
    relationships: dict[CanonicalLogicalId, PhysicalRelationshipBinding] = Field(
        default_factory=dict
    )
    policies: EnterprisePolicies = Field(default_factory=EnterprisePolicies)


class EnterpriseDataBinding(PackModel):
    api_version: Literal["dataagent.io/enterprise/v1"]
    kind: Literal["EnterpriseDataBinding"]
    metadata: PackMetadata
    spec: EnterpriseBindingSpec


class RuntimeLimits(PackModel):
    max_tool_calls: int = Field(default=24, ge=1)
    max_correction_rounds: int = Field(default=2, ge=0)
    max_sql_compile_attempts: int = Field(default=3, ge=1)
    max_duration_seconds: int = Field(default=120, ge=1)
    max_result_rows: int = Field(default=1000, ge=1)


class DeploymentProfileSpec(PackModel):
    enterprise_pack: PackReferenceText
    environment: NonBlankText
    secrets_provider: Literal["environment"]
    datasource_secrets: dict[SecretReference, EnvironmentVariable] = Field(
        min_length=1
    )
    memory_database_ref: EnvironmentVariable | None = None
    runtime: RuntimeLimits = Field(default_factory=RuntimeLimits)

    @field_validator("datasource_secrets")
    @classmethod
    def validate_datasource_secret_refs(
        cls, value: dict[SecretReference, EnvironmentVariable]
    ) -> dict[SecretReference, EnvironmentVariable]:
        secret_pattern = re.compile(
            r"^secret://[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+$"
        )
        invalid = [reference for reference in value if not secret_pattern.fullmatch(reference)]
        if invalid:
            raise ValueError("datasourceSecrets keys must be secret:// references")
        return value


class DeploymentProfile(PackModel):
    api_version: Literal["dataagent.io/deployment/v1"]
    kind: Literal["DeploymentProfile"]
    metadata: PackMetadata
    spec: DeploymentProfileSpec
