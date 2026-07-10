"""Deterministic compilation of validated packs into runtime bundles."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .packs import DeploymentProfile, DomainPack, EnterpriseDataBinding


def _json_compatible(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_compatible(
            value.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible configuration in one canonical form."""

    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalized(value: Any) -> Any:
    return json.loads(canonical_json(value))


class FrozenDict(dict):
    """JSON-compatible dictionary that rejects all in-place mutation."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("resolved runtime bundle content is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __copy__(self) -> "FrozenDict":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenDict":
        return self


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


class ResolvedRuntimeBundle(BaseModel):
    """Immutable, secret-free snapshot loaded atomically by a runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_version: str
    domain_pack_digest: str
    enterprise_binding_digest: str
    deployment_profile_digest: str
    skill_versions: dict[str, str] = Field(default_factory=dict)
    tool_registry_version: str
    semantic_model: dict[str, Any]
    physical_bindings: dict[str, Any]
    connector_capabilities: dict[str, Any]
    compiled_access_policy: dict[str, Any]
    runtime_limits: dict[str, Any]
    schema_fingerprint: str
    digest: str

    def model_post_init(self, context: Any) -> None:
        mapping_fields = (
            "skill_versions",
            "semantic_model",
            "physical_bindings",
            "connector_capabilities",
            "compiled_access_policy",
            "runtime_limits",
        )
        for field_name in mapping_fields:
            object.__setattr__(
                self,
                field_name,
                _deep_freeze(getattr(self, field_name)),
            )

    @model_validator(mode="after")
    def verify_digest(self) -> "ResolvedRuntimeBundle":
        payload = {
            field_name: getattr(self, field_name)
            for field_name in type(self).model_fields
            if field_name != "digest"
        }
        expected_digest = stable_digest(payload)
        if not hmac.compare_digest(self.digest, expected_digest):
            raise ValueError("resolved runtime bundle digest does not match content")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> "ResolvedRuntimeBundle":
        if update:
            raise TypeError("resolved runtime bundles cannot be updated in place")
        return super().model_copy(deep=deep)


def _pack_ref(name: str, version: str) -> str:
    return f"{name}@{version}"


def _validate_pack_references(
    domain_pack: DomainPack,
    enterprise_binding: EnterpriseDataBinding,
    deployment_profile: DeploymentProfile,
) -> None:
    domain_ref = _pack_ref(
        domain_pack.metadata.name,
        domain_pack.metadata.version,
    )
    declared_domains = tuple(item.ref for item in enterprise_binding.spec.domains)
    if declared_domains != (domain_ref,):
        raise ValueError(
            "enterprise binding must reference exactly one domain pack: "
            f"{domain_ref!r}"
        )

    enterprise_ref = _pack_ref(
        enterprise_binding.metadata.name,
        enterprise_binding.metadata.version,
    )
    if deployment_profile.spec.enterprise_pack != enterprise_ref:
        raise ValueError(
            "deployment profile enterprise pack does not match "
            f"{enterprise_ref!r}"
        )

    configured_secrets = set(deployment_profile.spec.datasource_secrets)
    missing_secrets = {
        source.connection_ref
        for source in enterprise_binding.spec.sources.values()
        if source.connection_ref not in configured_secrets
    }
    if missing_secrets:
        raise ValueError("deployment profile does not resolve every source secret")

    declared_sources = set(enterprise_binding.spec.sources)
    declared_entities = domain_pack.spec.entities
    relation_allowlist = set(
        enterprise_binding.spec.policies.relation_allowlist
    )
    for entity_id, binding in enterprise_binding.spec.bindings.items():
        if binding.source not in declared_sources:
            raise ValueError(f"binding {entity_id!r} references a missing source")
        if entity_id not in declared_entities:
            raise ValueError(f"binding {entity_id!r} references a missing entity")
        canonical_fields = set(declared_entities[entity_id].fields)
        if set(binding.fields) - canonical_fields:
            raise ValueError(f"binding {entity_id!r} references missing fields")
        if tuple(binding.grain) != tuple(declared_entities[entity_id].grain):
            raise ValueError(f"binding {entity_id!r} grain does not match domain")
        if set(binding.grain) - set(binding.fields):
            raise ValueError(f"binding {entity_id!r} grain is not fully mapped")
        if relation_allowlist and binding.relation not in relation_allowlist:
            raise ValueError(f"binding {entity_id!r} relation is not allowed")

    tenant_scope = enterprise_binding.spec.policies.tenant_scope
    declared_domain_fields = {
        f"{entity_id}.{field_name}"
        for entity_id, entity in declared_entities.items()
        for field_name in entity.fields
    }
    if (
        tenant_scope is not None
        and tenant_scope.canonical_field not in declared_domain_fields
    ):
        raise ValueError("tenant scope references a missing canonical field")
    if tenant_scope is not None:
        tenant_entity, tenant_field = tenant_scope.canonical_field.rsplit(".", 1)
        tenant_binding = enterprise_binding.spec.bindings.get(tenant_entity)
        if tenant_binding is None or tenant_field not in tenant_binding.fields:
            raise ValueError("tenant scope has no physical field mapping")


def compile_runtime_bundle(
    domain_pack: DomainPack,
    enterprise_binding: EnterpriseDataBinding,
    deployment_profile: DeploymentProfile,
    *,
    runtime_version: str,
    skill_versions: Mapping[str, str],
    tool_registry_version: str,
    schema_fingerprint: str,
) -> ResolvedRuntimeBundle:
    """Validate references and produce a deterministic, secret-free bundle."""

    _validate_pack_references(domain_pack, enterprise_binding, deployment_profile)

    payload = {
        "runtime_version": runtime_version,
        "domain_pack_digest": stable_digest(domain_pack),
        "enterprise_binding_digest": stable_digest(enterprise_binding),
        "deployment_profile_digest": stable_digest(deployment_profile),
        "skill_versions": _normalized(dict(skill_versions)),
        "tool_registry_version": tool_registry_version,
        "semantic_model": _normalized(domain_pack.spec),
        "physical_bindings": _normalized(enterprise_binding.spec.bindings),
        "connector_capabilities": _normalized(
            {
                name: {
                    "connector": source.connector,
                    "read_only": source.read_only,
                }
                for name, source in enterprise_binding.spec.sources.items()
            }
        ),
        "compiled_access_policy": _normalized(enterprise_binding.spec.policies),
        "runtime_limits": _normalized(deployment_profile.spec.runtime),
        "schema_fingerprint": schema_fingerprint,
    }
    return ResolvedRuntimeBundle(
        **payload,
        digest=stable_digest(payload),
    )
