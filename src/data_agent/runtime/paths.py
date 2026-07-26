"""Deterministic discovery of governed Runtime data files."""

from __future__ import annotations

import json
import os
import sysconfig
from collections.abc import Mapping
from pathlib import Path

from .bundle_store import BundlePaths
from .profile_loader import enterprise_binding_source_paths


PROJECT_ROOT_ENV = "DATA_AGENT_PROJECT_ROOT"
BUNDLE_PATHS_ENV = "DATA_AGENT_BUNDLE_PATHS_FILE"
_BUNDLE_PATH_FIELDS = (
    "domain_root",
    "enterprise_root",
    "deployment_profile",
    "pack_lock",
    "schema_catalog",
    "bundle_manifest",
)
_SOURCE_REQUIRED_FILES = (
    "packs/domains/commerce/pack.yaml",
    "packs/domains/commerce/semantic-model.yaml",
    "packs/domains/commerce/metrics.yaml",
    "packs/domains/commerce/vocabulary.zh-CN.yaml",
    "packs/domains/commerce/policies.yaml",
    "packs/domains/commerce/evals.yaml",
    "packs/enterprises/olist/pack.yaml",
    "packs/enterprises/olist/pack.lock",
    "packs/deployments/olist-local.yaml",
    "schema_catalog.json",
)
_RUNTIME_REQUIRED_FILES = (
    *_SOURCE_REQUIRED_FILES,
    "generated/bundles/olist-local.json",
)


def installed_data_root() -> Path:
    """Return the platform-specific root populated by wheel ``data-files``."""

    return Path(sysconfig.get_path("data")) / "share" / "data-agent"


def is_valid_project_root(candidate: str | Path) -> bool:
    """Return whether ``candidate`` can activate the published Runtime."""

    return _has_required_files(candidate, _RUNTIME_REQUIRED_FILES)


def is_valid_source_root(candidate: str | Path) -> bool:
    """Return whether ``candidate`` can compile governed source artifacts."""

    return _has_required_files(candidate, _SOURCE_REQUIRED_FILES)


def _has_required_files(candidate: str | Path, required_files: tuple[str, ...]) -> bool:
    root = Path(candidate).expanduser()
    if not root.is_dir() or not all(
        (root / relative).is_file() for relative in required_files
    ):
        return False
    try:
        enterprise_sources = enterprise_binding_source_paths(
            root / "packs" / "enterprises" / "olist"
        )
    except (OSError, TypeError, ValueError):
        return False
    return all(path.is_file() for path in enterprise_sources)


def resolve_project_root(
    project_root: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    installed_root: str | Path | None = None,
    development_root: str | Path | None = None,
) -> Path:
    """Resolve Runtime data with one explicit, deterministic precedence order.

    Explicit and environment-provided roots are authoritative and therefore fail
    closed when incomplete. Automatic candidates are considered only when valid.
    """

    if project_root is not None:
        return _require_valid_root(
            project_root,
            "explicit project root",
            _RUNTIME_REQUIRED_FILES,
        )

    env = os.environ if environment is None else environment
    configured = env.get(PROJECT_ROOT_ENV)
    if configured:
        return _require_valid_root(configured, PROJECT_ROOT_ENV, _RUNTIME_REQUIRED_FILES)

    working_root = Path.cwd() if cwd is None else Path(cwd)
    if is_valid_project_root(working_root):
        return working_root.expanduser().resolve()

    packaged_root = installed_data_root() if installed_root is None else Path(installed_root)
    if is_valid_project_root(packaged_root):
        return packaged_root.expanduser().resolve()

    repository_root = (
        Path(__file__).resolve().parents[3]
        if development_root is None
        else Path(development_root)
    )
    if is_valid_project_root(repository_root):
        return repository_root.expanduser().resolve()

    raise FileNotFoundError(
        "Data Agent Runtime data was not found in cwd, the installed data root, "
        "or the development repository"
    )


def resolve_source_root(
    project_root: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    installed_root: str | Path | None = None,
    development_root: str | Path | None = None,
) -> Path:
    """Resolve a pack/catalog source root without requiring published outputs."""

    if project_root is not None:
        return _require_valid_root(
            project_root,
            "explicit source root",
            _SOURCE_REQUIRED_FILES,
        )

    env = os.environ if environment is None else environment
    configured = env.get(PROJECT_ROOT_ENV)
    if configured:
        return _require_valid_root(
            configured,
            PROJECT_ROOT_ENV,
            _SOURCE_REQUIRED_FILES,
        )

    candidates = (
        Path.cwd() if cwd is None else Path(cwd),
        installed_data_root() if installed_root is None else Path(installed_root),
        Path(__file__).resolve().parents[3]
        if development_root is None
        else Path(development_root),
    )
    for candidate in candidates:
        if is_valid_source_root(candidate):
            return candidate.expanduser().resolve()
    raise FileNotFoundError(
        "Data Agent source data was not found in cwd, the installed data root, "
        "or the development repository"
    )


def bundle_paths(project_root: str | Path) -> BundlePaths:
    root = Path(project_root)
    return BundlePaths(
        domain_root=root / "packs" / "domains" / "commerce",
        enterprise_root=root / "packs" / "enterprises" / "olist",
        deployment_profile=root / "packs" / "deployments" / "olist-local.yaml",
        pack_lock=root / "packs" / "enterprises" / "olist" / "pack.lock",
        schema_catalog=root / "schema_catalog.json",
        bundle_manifest=root / "generated" / "bundles" / "olist-local.json",
    )


def resolve_bundle_paths(
    project_root: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> BundlePaths:
    """Resolve either an external bundle descriptor or the bundled OList demo.

    An explicit project root keeps the backwards-compatible repository layout.
    Otherwise ``DATA_AGENT_BUNDLE_PATHS_FILE`` may point at a strict JSON object
    containing all six ``BundlePaths`` fields. Relative values are resolved from
    the descriptor's directory.
    """

    if project_root is not None:
        return validate_bundle_paths(
            bundle_paths(
                resolve_project_root(project_root, environment=environment)
            )
        )
    env = os.environ if environment is None else environment
    descriptor = env.get(BUNDLE_PATHS_ENV)
    if descriptor:
        return _load_bundle_paths_file(descriptor)
    return validate_bundle_paths(
        bundle_paths(resolve_project_root(environment=environment))
    )


def validate_bundle_paths(paths: BundlePaths) -> BundlePaths:
    """Resolve and fail closed when any governed bundle input is missing."""

    resolved = BundlePaths(
        **{
            field: Path(getattr(paths, field)).expanduser().resolve()
            for field in _BUNDLE_PATH_FIELDS
        }
    )
    missing = [
        field
        for field in ("domain_root", "enterprise_root")
        if not getattr(resolved, field).is_dir()
    ]
    missing.extend(
        field
        for field in (
            "deployment_profile",
            "pack_lock",
            "schema_catalog",
            "bundle_manifest",
        )
        if not getattr(resolved, field).is_file()
    )
    if missing:
        raise FileNotFoundError(
            "bundle paths are incomplete: " + ", ".join(missing)
        )
    return resolved


def _load_bundle_paths_file(descriptor: str | Path) -> BundlePaths:
    descriptor_path = Path(descriptor).expanduser().resolve()
    try:
        document = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{BUNDLE_PATHS_ENV} is not a readable JSON descriptor"
        ) from exc
    if not isinstance(document, dict) or set(document) != set(
        _BUNDLE_PATH_FIELDS
    ):
        raise ValueError(
            f"{BUNDLE_PATHS_ENV} must contain exactly: "
            + ", ".join(_BUNDLE_PATH_FIELDS)
        )
    root = descriptor_path.parent
    values: dict[str, Path] = {}
    for field in _BUNDLE_PATH_FIELDS:
        value = document[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{BUNDLE_PATHS_ENV}.{field} must be a path")
        path = Path(value.strip()).expanduser()
        values[field] = path if path.is_absolute() else root / path
    return validate_bundle_paths(BundlePaths(**values))


def _require_valid_root(
    candidate: str | Path,
    source: str,
    required_files: tuple[str, ...],
) -> Path:
    root = Path(candidate).expanduser().resolve()
    if not _has_required_files(root, required_files):
        missing = [relative for relative in required_files if not (root / relative).is_file()]
        enterprise_root = root / "packs" / "enterprises" / "olist"
        try:
            missing.extend(
                str(path.relative_to(root)).replace("\\", "/")
                for path in enterprise_binding_source_paths(enterprise_root)
                if not path.is_file()
            )
        except (OSError, TypeError, ValueError):
            if "packs/enterprises/olist/pack.yaml" not in missing:
                missing.append("packs/enterprises/olist/pack.yaml (invalid)")
        details = ", ".join(missing)
        raise FileNotFoundError(f"{source} is not a complete Data Agent data root: {details}")
    return root


__all__ = [
    "BUNDLE_PATHS_ENV",
    "PROJECT_ROOT_ENV",
    "bundle_paths",
    "installed_data_root",
    "is_valid_source_root",
    "is_valid_project_root",
    "resolve_bundle_paths",
    "resolve_project_root",
    "resolve_source_root",
    "validate_bundle_paths",
]
