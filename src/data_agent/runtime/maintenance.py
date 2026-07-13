"""Packaged Runtime maintenance operations used by the CLI and dev scripts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .composition import (
    canonical_json,
    compile_runtime_bundle,
    load_bundle_manifest,
    stable_digest,
    write_bundle_manifest,
)
from .packs import DeploymentProfile, DomainPack
from .paths import resolve_source_root
from .profile_loader import (
    load_domain_pack,
    load_enterprise_binding,
    load_pack_yaml,
)


_FORBIDDEN = (
    "olist_",
    "public.",
    "secret://",
    "postgresql://",
    "connectionref",
    "physical_bindings",
    "relationallowlist",
)


def compile_packs(
    *,
    project_root: str | Path | None = None,
    output_path: str | Path | None = None,
    schema_catalog: str | Path | None = None,
    pack_lock: str | Path | None = None,
) -> Path:
    root = resolve_source_root(project_root)
    domain_root = root / "packs" / "domains" / "commerce"
    enterprise_root = root / "packs" / "enterprises" / "olist"
    deployment_path = root / "packs" / "deployments" / "olist-local.yaml"
    catalog_path = (
        Path(schema_catalog) if schema_catalog is not None else root / "schema_catalog.json"
    )
    lock_path = Path(pack_lock) if pack_lock is not None else enterprise_root / "pack.lock"
    destination = (
        Path(output_path)
        if output_path is not None
        else root / "generated" / "bundles" / "olist-local.json"
    )

    domain = load_domain_pack(domain_root)
    enterprise = load_enterprise_binding(enterprise_root)
    deployment = load_pack_yaml(deployment_path, DeploymentProfile)
    bundle = compile_runtime_bundle(
        domain,
        enterprise,
        deployment,
        runtime_version="1.0.0",
        skill_versions={"commerce.analytics": "1.0.0"},
        tool_registry_version="1.0.0",
        schema_catalog=catalog_path,
        pack_lock=lock_path,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        write_bundle_manifest(
            bundle,
            temporary,
            domain_ref=f"{domain.metadata.name}@{domain.metadata.version}",
            enterprise_ref=f"{enterprise.metadata.name}@{enterprise.metadata.version}",
            deployment_ref=f"{deployment.metadata.name}@{deployment.metadata.version}",
        )
        verified = load_bundle_manifest(
            temporary,
            pack_lock=lock_path,
            schema_catalog=catalog_path,
        )
        if verified != bundle:
            raise ValueError("published bundle did not round-trip through verification")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def build_semantic_index(domain: DomainPack) -> dict[str, Any]:
    terms: dict[str, list[dict[str, str]]] = {}
    for vocabulary in domain.spec.vocabulary:
        for ref in vocabulary.refs:
            terms.setdefault(ref, []).append(
                {"locale": vocabulary.locale, "term": vocabulary.term}
            )

    entries: list[dict[str, Any]] = []
    for entity_id, entity in domain.spec.entities.items():
        entries.append(
            _entry(
                ref=entity_id,
                kind="entity",
                label=entity_id.rsplit(".", 1)[-1],
                description=entity.description,
                terms=terms.get(entity_id, ()),
            )
        )
        for field_name, field in entity.fields.items():
            ref = f"{entity_id}.{field_name}"
            entries.append(
                _entry(
                    ref=ref,
                    kind="field",
                    label=field_name,
                    description=field.description or "",
                    terms=terms.get(ref, ()),
                    attributes={
                        "type": field.type,
                        "nullable": field.nullable,
                        "unit": field.unit,
                        "timeSemantics": field.time_semantics,
                    },
                )
            )
    for metric_id, metric in domain.spec.metrics.items():
        entries.append(
            _entry(
                ref=metric_id,
                kind="metric",
                label=metric_id.rsplit(".", 1)[-1],
                description=metric.description,
                terms=terms.get(metric_id, ()),
                attributes={
                    "aggregation": metric.aggregation,
                    "inputs": list(metric.inputs),
                    "eventTime": metric.event_time,
                },
            )
        )
    for relationship in domain.spec.relationships:
        entries.append(
            _entry(
                ref=relationship.name,
                kind="relationship",
                label=relationship.name.rsplit(".", 1)[-1],
                description=(
                    f"{relationship.from_entity} to {relationship.to_entity} "
                    f"({relationship.cardinality})"
                ),
                terms=terms.get(relationship.name, ()),
                attributes={
                    "fromEntity": relationship.from_entity,
                    "fromFields": list(relationship.from_fields),
                    "toEntity": relationship.to_entity,
                    "toFields": list(relationship.to_fields),
                    "cardinality": relationship.cardinality,
                },
            )
        )
    for policy in domain.spec.policies:
        entries.append(
            _entry(
                ref=policy.name,
                kind="policy",
                label=policy.name.rsplit(".", 1)[-1],
                description=policy.description,
                terms=terms.get(policy.name, ()),
            )
        )
    entries.sort(key=lambda item: (item["kind"], item["ref"]))
    payload: dict[str, Any] = {
        "apiVersion": "dataagent.io/semantic-index/v1",
        "kind": "CanonicalSemanticIndex",
        "domainPack": f"{domain.metadata.name}@{domain.metadata.version}",
        "domainDigest": stable_digest(domain),
        "entries": entries,
    }
    payload["digest"] = stable_digest(payload)
    _verify_canonical_only(payload)
    return payload


def rebuild_semantic_index(
    *,
    project_root: str | Path | None = None,
    output_path: str | Path | None = None,
) -> Path:
    root = resolve_source_root(project_root)
    domain = load_domain_pack(root / "packs" / "domains" / "commerce")
    document = build_semantic_index(domain)
    destination = (
        Path(output_path)
        if output_path is not None
        else root / "generated" / "semantic" / "commerce.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(canonical_json(document) + "\n", encoding="utf-8")
        loaded = json.loads(temporary.read_text(encoding="utf-8"))
        _verify_canonical_only(loaded)
        if loaded != document:
            raise ValueError("semantic index did not round-trip canonically")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _entry(
    *,
    ref: str,
    kind: str,
    label: str,
    description: str,
    terms: Any,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "ref": ref,
        "kind": kind,
        "label": label,
        "description": description,
        "terms": sorted(
            (dict(term) for term in terms),
            key=lambda value: (value["locale"], value["term"]),
        ),
    }
    if attributes:
        item["attributes"] = {
            key: value for key, value in sorted(attributes.items()) if value is not None
        }
    return item


def _verify_canonical_only(document: dict[str, Any]) -> None:
    digest = document.get("digest")
    unsigned = dict(document)
    unsigned.pop("digest", None)
    if digest != stable_digest(unsigned):
        raise ValueError("semantic index digest does not match canonical content")
    serialized = canonical_json(document).casefold()
    if any(value in serialized for value in _FORBIDDEN):
        raise ValueError("semantic index contains physical or secret configuration")
    entries = document.get("entries")
    if not isinstance(entries, list) or entries != sorted(
        entries,
        key=lambda item: (item["kind"], item["ref"]),
    ):
        raise ValueError("semantic index entries are not canonical")


__all__ = ["build_semantic_index", "compile_packs", "rebuild_semantic_index"]
