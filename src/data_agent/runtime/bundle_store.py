"""Verified immutable bundle snapshots with atomic, one-shot activation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from threading import Lock

from .composition import (
    ResolvedRuntimeBundle,
    load_bundle_manifest,
    stable_digest,
)
from .packs import DeploymentProfile, DomainPack, EnterpriseDataBinding
from .profile_loader import (
    domain_pack_source_paths,
    enterprise_binding_source_paths,
    load_domain_pack,
    load_enterprise_binding,
    load_pack_yaml,
)


@dataclass(frozen=True, slots=True)
class BundlePaths:
    domain_root: Path
    enterprise_root: Path
    deployment_profile: Path
    pack_lock: Path
    schema_catalog: Path
    bundle_manifest: Path


@dataclass(frozen=True, slots=True)
class SourceAttestation:
    """One canonical source identifier and its exact raw-byte digest."""

    source_id: "BundleSourceId"
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, BundleSourceId):
            raise TypeError("source attestation requires a canonical source id")
        if (
            len(self.sha256) != 64
            or self.sha256 != self.sha256.lower()
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("source attestation requires a lowercase SHA-256 digest")


class BundleSourceId(StrEnum):
    DOMAIN_PACK = "domain/pack.yaml"
    DOMAIN_SEMANTIC_MODEL = "domain/semantic-model.yaml"
    DOMAIN_METRICS = "domain/metrics.yaml"
    DOMAIN_VOCABULARY_ZH_CN = "domain/vocabulary.zh-CN.yaml"
    DOMAIN_POLICIES = "domain/policies.yaml"
    DOMAIN_EVALS = "domain/evals.yaml"
    ENTERPRISE_PACK = "enterprise/pack.yaml"
    ENTERPRISE_SOURCES = "enterprise/sources.yaml"
    ENTERPRISE_COMMERCE_BINDING = "enterprise/bindings/commerce.yaml"
    ENTERPRISE_POLICIES = "enterprise/policies.yaml"
    ENTERPRISE_PACK_LOCK = "enterprise/pack.lock"
    DEPLOYMENT_PROFILE = "deployment/profile.yaml"
    SCHEMA_CATALOG = "schema/catalog.json"
    BUNDLE_MANIFEST = "bundle/manifest.json"


@dataclass(frozen=True, slots=True)
class BundleAttestations:
    """Path-free attestations for the complete source set used at stage time."""

    sources: tuple[SourceAttestation, ...]

    def __post_init__(self) -> None:
        source_ids = tuple(item.source_id for item in self.sources)
        if not self.sources or len(source_ids) != len(set(source_ids)):
            raise ValueError("bundle source attestations must be non-empty and unique")
        if source_ids != tuple(sorted(source_ids, key=str)):
            raise ValueError("bundle source attestations must use canonical ordering")

    def digest_for(self, source_id: BundleSourceId) -> str:
        try:
            return next(
                item.sha256 for item in self.sources if item.source_id == source_id
            )
        except StopIteration as exc:
            raise KeyError(source_id) from exc

    @property
    def pack_lock_digest(self) -> str:
        return self.digest_for(BundleSourceId.ENTERPRISE_PACK_LOCK)

    @property
    def schema_catalog_digest(self) -> str:
        return self.digest_for(BundleSourceId.SCHEMA_CATALOG)

    @property
    def bundle_manifest_digest(self) -> str:
        return self.digest_for(BundleSourceId.BUNDLE_MANIFEST)


@dataclass(frozen=True, slots=True)
class BundleSnapshot:
    domain_pack: DomainPack
    enterprise_binding: EnterpriseDataBinding
    deployment_profile: DeploymentProfile
    bundle: ResolvedRuntimeBundle
    attestations: BundleAttestations
    generation: int = 0

    @property
    def domain_pack_digest(self) -> str:
        return stable_digest(self.domain_pack)

    @property
    def enterprise_binding_digest(self) -> str:
        return stable_digest(self.enterprise_binding)

    @property
    def deployment_profile_digest(self) -> str:
        return stable_digest(self.deployment_profile)


_CANDIDATE_FACTORY_KEY = object()


class VerifiedBundleCandidate:
    """Opaque store-scoped capability returned only after full validation."""

    __slots__ = ()

    def __new__(cls, factory_key: object = None) -> "VerifiedBundleCandidate":
        if factory_key is not _CANDIDATE_FACTORY_KEY:
            raise TypeError("verified bundle candidates are issued by BundleStore")
        return super().__new__(cls)


@dataclass(frozen=True, slots=True)
class _SourcePath:
    source_id: BundleSourceId
    path: Path

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("candidate source paths must be absolute")


@dataclass(frozen=True, slots=True)
class _CandidateRecord:
    paths: BundlePaths
    source_paths: tuple[_SourcePath, ...]
    snapshot: BundleSnapshot


class BundleNotActiveError(LookupError):
    """Raised when a runtime starts before a verified bundle is active."""


class BundleStore:
    """Validate off-lock, then atomically activate one store-issued capability."""

    __slots__ = ("_active", "_candidates", "_generation", "_lock")

    def __init__(self) -> None:
        self._active: BundleSnapshot | None = None
        self._candidates: dict[VerifiedBundleCandidate, _CandidateRecord] = {}
        self._generation = 0
        self._lock = Lock()

    def load(self, paths: BundlePaths) -> VerifiedBundleCandidate:
        """Fully validate sources and return an opaque activation capability."""

        return self.stage(paths)

    def stage(self, paths: BundlePaths) -> VerifiedBundleCandidate:
        normalized = self._normalized_paths(paths)
        source_paths = self._source_paths(normalized)
        snapshot = self._load_verified_snapshot(normalized, source_paths)
        candidate = VerifiedBundleCandidate(_CANDIDATE_FACTORY_KEY)
        with self._lock:
            self._candidates[candidate] = _CandidateRecord(
                paths=normalized,
                source_paths=source_paths,
                snapshot=snapshot,
            )
        return candidate

    def verify(self, candidate: VerifiedBundleCandidate) -> bool:
        """Revalidate a live candidate without consuming its authority."""

        record = self._candidate_record(candidate, consume=False)
        fresh = self._load_verified_snapshot(record.paths, record.source_paths)
        if fresh != record.snapshot:
            raise ValueError("bundle candidate sources changed after validation")
        with self._lock:
            if self._candidates.get(candidate) is not record:
                raise ValueError("bundle candidate is no longer valid")
        return True

    def activate(self, candidate: VerifiedBundleCandidate) -> BundleSnapshot:
        """Consume one verified capability and atomically swap on revalidation."""

        record = self._candidate_record(candidate, consume=True)
        fresh = self._load_verified_snapshot(record.paths, record.source_paths)
        if fresh != record.snapshot:
            raise ValueError("bundle candidate sources changed after validation")
        if (
            self._source_paths(record.paths) != record.source_paths
            or self._attest_sources(record.source_paths) != fresh.attestations
        ):
            raise ValueError("bundle candidate sources changed during activation")
        with self._lock:
            generation = self._generation + 1
            active = replace(fresh, generation=generation)
            self._active = active
            self._generation = generation
            return active

    def load_and_activate(self, paths: BundlePaths) -> BundleSnapshot:
        return self.activate(self.stage(paths))

    def snapshot(self) -> BundleSnapshot:
        with self._lock:
            active = self._active
        if active is None:
            raise BundleNotActiveError("no verified runtime bundle is active")
        return active

    def _candidate_record(
        self,
        candidate: VerifiedBundleCandidate,
        *,
        consume: bool,
    ) -> _CandidateRecord:
        if not isinstance(candidate, VerifiedBundleCandidate):
            raise TypeError("bundle activation requires a verified candidate")
        with self._lock:
            record = (
                self._candidates.pop(candidate, None)
                if consume
                else self._candidates.get(candidate)
            )
        if record is None:
            raise ValueError("bundle candidate is foreign, forged, or already consumed")
        return record

    @staticmethod
    def _normalized_paths(paths: BundlePaths) -> BundlePaths:
        if not isinstance(paths, BundlePaths):
            raise TypeError("bundle staging requires BundlePaths")
        return BundlePaths(
            domain_root=Path(paths.domain_root).resolve(strict=True),
            enterprise_root=Path(paths.enterprise_root).resolve(strict=True),
            deployment_profile=Path(paths.deployment_profile).resolve(strict=True),
            pack_lock=Path(paths.pack_lock).resolve(strict=True),
            schema_catalog=Path(paths.schema_catalog).resolve(strict=True),
            bundle_manifest=Path(paths.bundle_manifest).resolve(strict=True),
        )

    @classmethod
    def _load_verified_snapshot(
        cls,
        paths: BundlePaths,
        source_paths: tuple[_SourcePath, ...],
    ) -> BundleSnapshot:
        if cls._source_paths(paths) != source_paths:
            raise ValueError("bundle candidate source set changed after staging")
        before = cls._attest_sources(source_paths)
        domain_pack = load_domain_pack(paths.domain_root)
        enterprise_binding = load_enterprise_binding(paths.enterprise_root)
        deployment_profile = load_pack_yaml(
            paths.deployment_profile,
            DeploymentProfile,
        )
        bundle = load_bundle_manifest(
            paths.bundle_manifest,
            pack_lock=paths.pack_lock,
            schema_catalog=paths.schema_catalog,
        )
        if cls._source_paths(paths) != source_paths:
            raise ValueError("bundle source set changed during validation")
        after = cls._attest_sources(source_paths)
        if before != after:
            raise ValueError("bundle sources changed during validation")
        snapshot = BundleSnapshot(
            domain_pack=domain_pack,
            enterprise_binding=enterprise_binding,
            deployment_profile=deployment_profile,
            bundle=bundle,
            attestations=after,
        )
        cls._verify_snapshot(snapshot)
        cls._verify_manifest_references(paths, snapshot)
        return snapshot

    @staticmethod
    def _verify_snapshot(snapshot: BundleSnapshot) -> None:
        bundle = ResolvedRuntimeBundle.model_validate(
            snapshot.bundle.model_dump(mode="json")
        )
        expected = {
            "domain_pack_digest": snapshot.domain_pack_digest,
            "enterprise_binding_digest": snapshot.enterprise_binding_digest,
            "deployment_profile_digest": snapshot.deployment_profile_digest,
        }
        for field_name, digest in expected.items():
            if getattr(bundle, field_name) != digest:
                raise ValueError(f"bundle {field_name} does not match loaded pack")

    @staticmethod
    def _verify_manifest_references(
        paths: BundlePaths,
        snapshot: BundleSnapshot,
    ) -> None:
        document = json.loads(paths.bundle_manifest.read_text(encoding="utf-8"))
        expected = {
            "domainPack": (
                f"{snapshot.domain_pack.metadata.name}@"
                f"{snapshot.domain_pack.metadata.version}"
            ),
            "enterprisePack": (
                f"{snapshot.enterprise_binding.metadata.name}@"
                f"{snapshot.enterprise_binding.metadata.version}"
            ),
            "deploymentProfile": (
                f"{snapshot.deployment_profile.metadata.name}@"
                f"{snapshot.deployment_profile.metadata.version}"
            ),
        }
        if any(document.get(name) != value for name, value in expected.items()):
            raise ValueError("bundle manifest pack references do not match loaded packs")

    @classmethod
    def _source_paths(cls, paths: BundlePaths) -> tuple[_SourcePath, ...]:
        try:
            domain_paths = tuple(
                path.resolve(strict=True)
                for path in domain_pack_source_paths(paths.domain_root)
            )
            enterprise_paths = tuple(
                path.resolve(strict=True)
                for path in enterprise_binding_source_paths(paths.enterprise_root)
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("could not resolve the bundle source set") from exc

        domain_ids = (
            BundleSourceId.DOMAIN_PACK,
            BundleSourceId.DOMAIN_SEMANTIC_MODEL,
            BundleSourceId.DOMAIN_METRICS,
            BundleSourceId.DOMAIN_VOCABULARY_ZH_CN,
            BundleSourceId.DOMAIN_POLICIES,
            BundleSourceId.DOMAIN_EVALS,
        )
        enterprise_ids = (
            (BundleSourceId.ENTERPRISE_PACK,)
            if len(enterprise_paths) == 1
            else (
                BundleSourceId.ENTERPRISE_PACK,
                BundleSourceId.ENTERPRISE_SOURCES,
                BundleSourceId.ENTERPRISE_COMMERCE_BINDING,
                BundleSourceId.ENTERPRISE_POLICIES,
            )
        )
        if len(domain_paths) != len(domain_ids) or len(enterprise_paths) != len(
            enterprise_ids
        ):
            raise ValueError("bundle loader returned an unsupported source set")
        pairs = (
            *zip(domain_ids, domain_paths, strict=True),
            *zip(enterprise_ids, enterprise_paths, strict=True),
            (BundleSourceId.ENTERPRISE_PACK_LOCK, paths.pack_lock),
            (BundleSourceId.DEPLOYMENT_PROFILE, paths.deployment_profile),
            (BundleSourceId.SCHEMA_CATALOG, paths.schema_catalog),
            (BundleSourceId.BUNDLE_MANIFEST, paths.bundle_manifest),
        )
        return tuple(
            _SourcePath(source_id=source_id, path=path)
            for source_id, path in sorted(pairs, key=lambda pair: str(pair[0]))
        )

    @classmethod
    def _attest_sources(
        cls,
        source_paths: tuple[_SourcePath, ...],
    ) -> BundleAttestations:
        return BundleAttestations(
            sources=tuple(
                SourceAttestation(
                    source_id=source.source_id,
                    sha256=cls._file_digest(source.path),
                )
                for source in source_paths
            ),
        )

    @staticmethod
    def _file_digest(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(f"could not attest bundle source {path}") from exc


__all__ = [
    "BundleAttestations",
    "BundleNotActiveError",
    "BundlePaths",
    "BundleSnapshot",
    "BundleStore",
    "BundleSourceId",
    "SourceAttestation",
    "VerifiedBundleCandidate",
]
