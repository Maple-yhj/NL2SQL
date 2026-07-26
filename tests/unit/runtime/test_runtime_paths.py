from __future__ import annotations

import json
import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from data_agent.runtime import paths


PROJECT_ROOT_ENV = paths.PROJECT_ROOT_ENV
BUNDLE_PATHS_ENV = paths.BUNDLE_PATHS_ENV
resolve_project_root = paths.resolve_project_root


SOURCE_FILES = (
    "packs/domains/commerce/pack.yaml",
    "packs/domains/commerce/semantic-model.yaml",
    "packs/domains/commerce/metrics.yaml",
    "packs/domains/commerce/vocabulary.zh-CN.yaml",
    "packs/domains/commerce/policies.yaml",
    "packs/domains/commerce/evals.yaml",
    "packs/enterprises/olist/pack.yaml",
    "packs/enterprises/olist/sources.yaml",
    "packs/enterprises/olist/bindings/commerce.yaml",
    "packs/enterprises/olist/policies.yaml",
    "packs/enterprises/olist/pack.lock",
    "packs/deployments/olist-local.yaml",
    "schema_catalog.json",
)
RUNTIME_MANIFEST = "generated/bundles/olist-local.json"
ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def temporary_root():
    root = ROOT / "generated" / "runtime-path-tests" / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def make_root(parent: Path, name: str, *, runtime: bool) -> Path:
    root = parent / name
    for relative in (*SOURCE_FILES, *((RUNTIME_MANIFEST,) if runtime else ())):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "spec: {}\n" if relative == "packs/enterprises/olist/pack.yaml" else "fixture\n"
        path.write_text(content, encoding="utf-8")
    return root


class RuntimePathTests(unittest.TestCase):
    def test_partial_cwd_does_not_shadow_complete_installed_runtime(self) -> None:
        with temporary_root() as parent:
            partial_cwd = make_root(parent, "partial-cwd", runtime=False)
            installed = make_root(parent, "installed", runtime=True)

            resolved = resolve_project_root(
                environment={},
                cwd=partial_cwd,
                installed_root=installed,
                development_root=parent / "missing-development",
            )

        self.assertEqual(resolved, installed.resolve())

    def test_explicit_and_environment_runtime_roots_fail_with_missing_manifest(self) -> None:
        with temporary_root() as parent:
            partial = make_root(parent, "source-only", runtime=False)
            attempts = (
                lambda: resolve_project_root(partial),
                lambda: resolve_project_root(
                    environment={PROJECT_ROOT_ENV: str(partial)},
                    cwd=parent / "missing-cwd",
                    installed_root=parent / "missing-installed",
                    development_root=parent / "missing-development",
                ),
            )
            for resolve in attempts:
                with self.subTest(source=resolve):
                    with self.assertRaisesRegex(
                        FileNotFoundError,
                        r"generated/bundles/olist-local\.json",
                    ):
                        resolve()

    def test_development_runtime_fallback_must_also_be_complete(self) -> None:
        with temporary_root() as parent:
            source_only = make_root(parent, "development", runtime=False)
            with self.assertRaises(FileNotFoundError):
                resolve_project_root(
                    environment={},
                    cwd=parent / "missing-cwd",
                    installed_root=parent / "missing-installed",
                    development_root=source_only,
                )

    def test_source_resolver_accepts_pack_catalog_root_without_published_bundle(self) -> None:
        with temporary_root() as parent:
            source_only = make_root(parent, "source-only", runtime=False)
            self.assertEqual(paths.resolve_source_root(source_only), source_only.resolve())

    def test_external_bundle_descriptor_resolves_relative_non_olist_paths(
        self,
    ) -> None:
        with temporary_root() as parent:
            domain_root = parent / "acme" / "domain"
            enterprise_root = parent / "acme" / "enterprise"
            domain_root.mkdir(parents=True)
            enterprise_root.mkdir(parents=True)
            file_paths = {
                "deployment_profile": parent / "acme" / "deployment.yaml",
                "pack_lock": enterprise_root / "pack.lock",
                "schema_catalog": parent / "acme" / "catalog.json",
                "bundle_manifest": parent / "acme" / "bundle.json",
            }
            for path in file_paths.values():
                path.write_text("fixture\n", encoding="utf-8")
            descriptor = parent / "bundle-paths.json"
            descriptor.write_text(
                json.dumps(
                    {
                        "domain_root": "acme/domain",
                        "enterprise_root": "acme/enterprise",
                        "deployment_profile": "acme/deployment.yaml",
                        "pack_lock": "acme/enterprise/pack.lock",
                        "schema_catalog": "acme/catalog.json",
                        "bundle_manifest": "acme/bundle.json",
                    }
                ),
                encoding="utf-8",
            )

            resolved = paths.resolve_bundle_paths(
                environment={BUNDLE_PATHS_ENV: str(descriptor)}
            )

        self.assertEqual(resolved.domain_root, domain_root.resolve())
        self.assertEqual(
            resolved.enterprise_root,
            enterprise_root.resolve(),
        )
        self.assertEqual(
            resolved.bundle_manifest,
            file_paths["bundle_manifest"].resolve(),
        )


if __name__ == "__main__":
    unittest.main()
