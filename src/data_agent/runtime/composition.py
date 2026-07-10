"""Deterministic compilation of validated packs into runtime bundles."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

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
    declared_domains = {item.ref for item in enterprise_binding.spec.domains}
    if domain_ref not in declared_domains:
        raise ValueError(
            f"enterprise binding does not reference domain pack {domain_ref!r}"
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
