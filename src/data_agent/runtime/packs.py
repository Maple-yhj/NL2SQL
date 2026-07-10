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
PackReferenceText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9_-]*@[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$",
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
    name: NonBlankText
    version: NonBlankText = "1.0.0"


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
    grain: tuple[NonBlankText, ...] = Field(min_length=1)
    fields: dict[NonBlankText, CanonicalField] = Field(default_factory=dict)
    description: NonBlankText | None = None


class CanonicalRelationship(PackModel):
    name: NonBlankText
    from_entity: NonBlankText
    to_entity: NonBlankText
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    from_fields: tuple[NonBlankText, ...] = Field(min_length=1)
    to_fields: tuple[NonBlankText, ...] = Field(min_length=1)


class CanonicalMetric(PackModel):
    aggregation: Literal["sum", "count", "count_distinct", "average", "min", "max"]
    inputs: tuple[NonBlankText, ...] = Field(min_length=1)
    combine: Literal["identity", "add"] = "identity"
    event_time: NonBlankText | None = None
    description: NonBlankText | None = None


class VocabularyEntry(PackModel):
    term: NonBlankText
    refs: tuple[NonBlankText, ...] = Field(min_length=1)
    locale: NonBlankText = "zh-CN"


class DomainPolicy(PackModel):
    name: NonBlankText
    description: NonBlankText


class DomainEvalCase(PackModel):
    id: NonBlankText
    question: NonBlankText
    expected_metrics: tuple[NonBlankText, ...] = ()
    expected_entities: tuple[NonBlankText, ...] = ()


class DomainPackSpec(PackModel):
    entities: dict[NonBlankText, CanonicalEntity] = Field(default_factory=dict)
    relationships: tuple[CanonicalRelationship, ...] = ()
    metrics: dict[NonBlankText, CanonicalMetric] = Field(default_factory=dict)
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


class PackReference(PackModel):
    ref: PackReferenceText


class EnterpriseSource(PackModel):
    connector: Literal["postgres"]
    connection_ref: SecretReference
    read_only: Literal[True]


class PhysicalFieldBinding(PackModel):
    column: NonBlankText
    cast: Literal["string", "integer", "decimal", "boolean", "date", "datetime"] | None = None
    timezone: NonBlankText | None = None
    null_policy: Literal["preserve", "reject", "coalesce"] = "preserve"
    enum_mapping: dict[NonBlankText, NonBlankText] = Field(default_factory=dict)


class PhysicalEntityBinding(PackModel):
    source: NonBlankText
    relation: NonBlankText
    grain: tuple[NonBlankText, ...] = Field(min_length=1)
    fields: dict[NonBlankText, PhysicalFieldBinding] = Field(default_factory=dict)


class TenantScopePolicy(PackModel):
    mode: NonBlankText
    canonical_field: NonBlankText
    principal_claim: NonBlankText


class EnterprisePolicies(PackModel):
    tenant_scope: TenantScopePolicy | None = None
    max_rows: int = Field(default=1000, ge=1)
    query_timeout_seconds: int = Field(default=10, ge=1)
    relation_allowlist: tuple[NonBlankText, ...] = ()


class EnterpriseBindingSpec(PackModel):
    domains: tuple[PackReference, ...] = Field(min_length=1)
    sources: dict[NonBlankText, EnterpriseSource] = Field(min_length=1)
    bindings: dict[NonBlankText, PhysicalEntityBinding] = Field(default_factory=dict)
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
    datasource_secrets: dict[str, EnvironmentVariable] = Field(min_length=1)
    memory_database_ref: EnvironmentVariable | None = None
    runtime: RuntimeLimits = Field(default_factory=RuntimeLimits)

    @field_validator("datasource_secrets")
    @classmethod
    def validate_datasource_secret_refs(
        cls, value: dict[str, EnvironmentVariable]
    ) -> dict[str, EnvironmentVariable]:
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
