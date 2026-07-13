from __future__ import annotations

import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from data_agent.runtime import paths


PROJECT_ROOT_ENV = paths.PROJECT_ROOT_ENV
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


if __name__ == "__main__":
    unittest.main()
