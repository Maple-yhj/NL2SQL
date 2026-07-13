"""Deterministic compilation of validated packs into runtime bundles."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .packs import (
    DeploymentProfile,
    DomainPack,
    EnterpriseDataBinding,
    EnterprisePackLock,
    _reject_executable_content,
)
from .schema_catalog import (
    load_schema_catalog,
    schema_fingerprint as compute_schema_fingerprint,
    validate_enterprise_binding_schema,
)


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


def _compiled_access_policy_digest(value: Any) -> str:
    """Hash exactly the canonical policy representation emitted in a bundle."""

    return stable_digest(_normalized(value))


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


def _load_pack_lock(
    value: str | Path | Mapping[str, Any],
) -> EnterprisePackLock:
    try:
        from .profile_loader import _load_yaml_mapping

        document = dict(value) if isinstance(value, Mapping) else _load_yaml_mapping(Path(value))
        return EnterprisePackLock.model_validate(document)
    except (OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        raise ValueError(f"could not load a valid enterprise pack lock {value}") from exc


def _validate_pack_lock(
    lock: EnterprisePackLock,
    domain_pack: DomainPack,
    enterprise_binding: EnterpriseDataBinding,
    computed_fingerprint: str,
    compiled_policy_digest: str,
) -> None:
    expected_enterprise = _pack_ref(
        enterprise_binding.metadata.name, enterprise_binding.metadata.version
    )
    expected_domain = _pack_ref(domain_pack.metadata.name, domain_pack.metadata.version)
    if lock.enterprise_pack != expected_enterprise:
        raise ValueError("enterprise pack lock does not match binding")
    if lock.access_mode != enterprise_binding.spec.policies.access_mode:
        raise ValueError("enterprise pack lock access mode does not match binding")
    if lock.domains != (expected_domain,):
        raise ValueError("enterprise pack lock domain reference does not match")
    if lock.schema_fingerprint != computed_fingerprint:
        raise ValueError("enterprise pack lock schema fingerprint is stale")
    expected_relations = set(enterprise_binding.spec.policies.relation_allowlist)
    locked_relations = set(lock.relations)
    if locked_relations != expected_relations:
        raise ValueError("enterprise pack lock relation allowlist is stale")
    if not hmac.compare_digest(lock.policy_digest, compiled_policy_digest):
        raise ValueError("enterprise pack lock policy digest is stale")


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
    if not relation_allowlist:
        raise ValueError("enterprise binding requires a relation allowlist")
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

    tenant_scope = getattr(enterprise_binding.spec.policies, "tenant_scope", None)
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
    if set(enterprise_binding.spec.bindings) != set(declared_entities):
        raise ValueError("enterprise binding must map every domain entity")

    for entity_id, binding in enterprise_binding.spec.bindings.items():
        canonical_fields = set(declared_entities[entity_id].fields)
        if set(binding.fields) != canonical_fields:
            raise ValueError(f"binding {entity_id!r} does not map every canonical field")

    # Every canonical relationship key must be physically resolvable. Explicit
    # relationship declarations are optional for backwards-compatible custom
    # packs, but when present they are checked against the domain graph.
    domain_relationships = {item.name: item for item in domain_pack.spec.relationships}
    policies = enterprise_binding.spec.policies
    if policies.access_mode == "tenant_scoped":
        tenant_scope = policies.tenant_scope
        if (
            tenant_scope.admin_bypass.principal_claim != "roles"
            or not tenant_scope.admin_bypass.allowed_roles
        ):
            raise ValueError("tenant-scoped policy requires role-based admin bypass")
        scope_entity = tenant_scope.canonical_field.rsplit(".", 1)[0]
        if set(tenant_scope.ownership_paths) != set(declared_entities):
            raise ValueError("tenant scope ownership paths must cover every entity")
        for entity_id in declared_entities:
            path = tenant_scope.ownership_paths[entity_id]
            current = entity_id
            for relationship_name in path:
                relationship = domain_relationships.get(relationship_name)
                if relationship is None:
                    raise ValueError("tenant scope ownership path references an unknown relationship")
                if relationship.from_entity == current:
                    current = relationship.to_entity
                elif relationship.to_entity == current:
                    current = relationship.from_entity
                else:
                    raise ValueError("tenant scope ownership path is disconnected")
            if current != scope_entity:
                raise ValueError("tenant scope ownership path does not end at seller scope")
    for name, relation in enterprise_binding.spec.relationships.items():
        canonical = domain_relationships.get(name)
        if canonical is None:
            raise ValueError(f"binding relationship {name!r} is not declared by domain")
        if (
            relation.from_entity != canonical.from_entity
            or relation.to_entity != canonical.to_entity
            or len(relation.from_columns) != len(canonical.from_fields)
            or len(relation.to_columns) != len(canonical.to_fields)
        ):
            raise ValueError(f"binding relationship {name!r} does not match domain")
        from_binding = enterprise_binding.spec.bindings.get(canonical.from_entity)
        to_binding = enterprise_binding.spec.bindings.get(canonical.to_entity)
        if from_binding is None or to_binding is None:
            raise ValueError(f"binding relationship {name!r} has no entity mapping")
        mapped_from_columns = {
            from_binding.fields[field].column for field in canonical.from_fields
        }
        mapped_to_columns = {
            to_binding.fields[field].column for field in canonical.to_fields
        }
        if set(relation.from_columns) != mapped_from_columns or set(
            relation.to_columns
        ) != mapped_to_columns:
            raise ValueError(f"binding relationship {name!r} keys do not match fields")
    if domain_relationships and set(enterprise_binding.spec.relationships) != set(
        domain_relationships
    ):
        raise ValueError("enterprise binding must declare every domain relationship")


def compile_runtime_bundle(
    domain_pack: DomainPack,
    enterprise_binding: EnterpriseDataBinding,
    deployment_profile: DeploymentProfile,
    *,
    runtime_version: str,
    skill_versions: Mapping[str, str],
    tool_registry_version: str,
    schema_fingerprint: str = "",
    schema_catalog: str | Path | list[Mapping[str, Any]] | None = None,
    pack_lock: str | Path | Mapping[str, Any] | None = None,
) -> ResolvedRuntimeBundle:
    """Validate references and produce a deterministic, secret-free bundle."""

    _validate_pack_references(domain_pack, enterprise_binding, deployment_profile)
    compiled_access_policy = _normalized(enterprise_binding.spec.policies)
    compiled_policy_digest = _compiled_access_policy_digest(compiled_access_policy)

    # OList deployments use the checked-in catalog by default. Callers may
    # provide an explicit catalog (or path) to validate a fresh introspection
    # result and detect drift before publication.
    catalog_was_explicit = schema_catalog is not None
    if schema_catalog is None:
        default_catalog = Path(__file__).resolve().parents[3] / "schema_catalog.json"
        if default_catalog.exists():
            schema_catalog = default_catalog
    if schema_catalog is None:
        raise ValueError("schema catalog is required for bundle compilation")
    catalog = (
        load_schema_catalog(schema_catalog)
        if isinstance(schema_catalog, (str, Path))
        else list(schema_catalog)
    )
    computed_fingerprint = validate_enterprise_binding_schema(
        domain_pack, enterprise_binding, catalog
    )
    fingerprint_is_digest = (
        len(schema_fingerprint) == 64
        and all(character in "0123456789abcdef" for character in schema_fingerprint)
    )
    if schema_fingerprint and (
        catalog_was_explicit
        or fingerprint_is_digest
        or enterprise_binding.spec.policies.access_mode == "tenant_scoped"
    ) and schema_fingerprint != computed_fingerprint:
        raise ValueError("schema fingerprint does not match catalog")
    schema_fingerprint = computed_fingerprint

    if pack_lock is None:
        pack_lock = (
            Path(__file__).resolve().parents[3]
            / "packs"
            / "enterprises"
            / enterprise_binding.metadata.name
            / "pack.lock"
        )
    if isinstance(pack_lock, (str, Path)) and not Path(pack_lock).exists():
        raise ValueError("enterprise pack lock is required for publication")
    _validate_pack_lock(
        _load_pack_lock(pack_lock),
        domain_pack,
        enterprise_binding,
        computed_fingerprint,
        compiled_policy_digest,
    )

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
        "compiled_access_policy": compiled_access_policy,
        "runtime_limits": _normalized(deployment_profile.spec.runtime),
        "schema_fingerprint": schema_fingerprint,
    }
    return ResolvedRuntimeBundle(
        **payload,
        digest=stable_digest(payload),
    )


def write_bundle_manifest(
    bundle: ResolvedRuntimeBundle,
    path: str | Path,
    *,
    domain_ref: str,
    enterprise_ref: str,
    deployment_ref: str,
) -> Path:
    """Write a deterministic, secret-free publication manifest for a bundle."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "apiVersion": "dataagent.io/bundle-manifest/v1",
        "kind": "ResolvedRuntimeBundleManifest",
        "domainPack": domain_ref,
        "enterprisePack": enterprise_ref,
        "deploymentProfile": deployment_ref,
        "schemaFingerprint": bundle.schema_fingerprint,
        "policyDigest": _compiled_access_policy_digest(bundle.compiled_access_policy),
        "bundleDigest": bundle.digest,
        "bundle": bundle.model_dump(mode="json"),
    }
    destination.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    return destination


def load_bundle_manifest(
    path: str | Path,
    *,
    pack_lock: str | Path | Mapping[str, Any],
    schema_catalog: str | Path | list[Mapping[str, Any]],
) -> ResolvedRuntimeBundle:
    """Read and verify a published bundle against its lock and catalog."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"bundle manifest has duplicate key {key!r}")
            result[key] = value
        return result

    try:
        document = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not load bundle manifest {path}") from exc
    if not isinstance(document, dict):
        raise ValueError("bundle manifest root must be an object")
    _reject_executable_content(document, "bundle manifest")
    if (
        document.get("apiVersion") != "dataagent.io/bundle-manifest/v1"
        or document.get("kind") != "ResolvedRuntimeBundleManifest"
    ):
        raise ValueError("bundle manifest version or kind is invalid")

    try:
        bundle = ResolvedRuntimeBundle.model_validate(document["bundle"])
    except (KeyError, ValueError) as exc:
        raise ValueError("bundle manifest contains an invalid runtime bundle") from exc
    if document.get("bundleDigest") != bundle.digest:
        raise ValueError("bundle manifest digest does not match runtime bundle")
    if document.get("schemaFingerprint") != bundle.schema_fingerprint:
        raise ValueError("bundle manifest schema fingerprint does not match runtime bundle")
    compiled_policy_digest = _compiled_access_policy_digest(
        bundle.compiled_access_policy
    )
    if document.get("policyDigest") != compiled_policy_digest:
        raise ValueError("bundle manifest policy digest does not match runtime bundle")

    catalog = (
        load_schema_catalog(schema_catalog)
        if isinstance(schema_catalog, (str, Path))
        else list(schema_catalog)
    )
    catalog_fingerprint = compute_schema_fingerprint(catalog)
    if bundle.schema_fingerprint != catalog_fingerprint:
        raise ValueError("bundle manifest schema fingerprint is stale")

    lock = _load_pack_lock(pack_lock)
    if lock.schema_fingerprint != catalog_fingerprint:
        raise ValueError("enterprise pack lock schema fingerprint is stale")
    if lock.enterprise_pack != document.get("enterprisePack"):
        raise ValueError("bundle manifest enterprise reference does not match lock")
    if lock.domains != (document.get("domainPack"),):
        raise ValueError("bundle manifest domain reference does not match lock")
    compiled_policy = bundle.compiled_access_policy
    if lock.access_mode != compiled_policy.get("accessMode"):
        raise ValueError("bundle manifest access mode does not match lock")
    if set(lock.relations) != set(compiled_policy.get("relationAllowlist", ())):
        raise ValueError("bundle manifest relation allowlist does not match lock")
    if not hmac.compare_digest(lock.policy_digest, compiled_policy_digest):
        raise ValueError("bundle manifest policy digest does not match lock")
    return bundle
