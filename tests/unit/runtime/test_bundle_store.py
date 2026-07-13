from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


class BundleStoreTests(unittest.TestCase):
    def paths(self, manifest: Path | None = None):
        from data_agent.runtime.bundle_store import BundlePaths

        return BundlePaths(
            domain_root=ROOT / "packs" / "domains" / "commerce",
            enterprise_root=ROOT / "packs" / "enterprises" / "olist",
            deployment_profile=ROOT / "packs" / "deployments" / "olist-local.yaml",
            pack_lock=ROOT / "packs" / "enterprises" / "olist" / "pack.lock",
            schema_catalog=ROOT / "schema_catalog.json",
            bundle_manifest=manifest or ROOT / "generated" / "bundles" / "olist-local.json",
        )

    def setUp(self) -> None:
        self.alternate_manifest = ROOT / "generated" / "bundles" / ".task8-review-bundle.json"
        self.fixture_root = ROOT / "generated" / ".task8-bundle-source-fixture"
        if self.alternate_manifest.exists():
            self.alternate_manifest.unlink()
        if self.fixture_root.exists():
            shutil.rmtree(self.fixture_root)

    def tearDown(self) -> None:
        if self.alternate_manifest.exists():
            self.alternate_manifest.unlink()
        if self.fixture_root.exists():
            shutil.rmtree(self.fixture_root)

    def isolated_paths(self):
        domain_root = self.fixture_root / "domain"
        enterprise_root = self.fixture_root / "enterprise"
        deployment_profile = self.fixture_root / "deployment.yaml"
        shutil.copytree(self.paths().domain_root, domain_root)
        shutil.copytree(self.paths().enterprise_root, enterprise_root)
        shutil.copy2(self.paths().deployment_profile, deployment_profile)
        return type(self.paths())(
            domain_root=domain_root,
            enterprise_root=enterprise_root,
            deployment_profile=deployment_profile,
            pack_lock=enterprise_root / "pack.lock",
            schema_catalog=self.paths().schema_catalog,
            bundle_manifest=self.paths().bundle_manifest,
        )

    def split_paths(self):
        paths = self.isolated_paths()
        document = yaml.safe_load(
            self.paths().enterprise_root.joinpath("pack.yaml").read_text(
                encoding="utf-8"
            )
        )
        spec = document.pop("spec")
        fragments = {
            paths.enterprise_root / "pack.yaml": document,
            paths.enterprise_root / "sources.yaml": {
                "domains": spec["domains"],
                "sources": spec["sources"],
            },
            paths.enterprise_root / "bindings" / "commerce.yaml": {
                "bindings": spec["bindings"],
                "relationships": spec["relationships"],
            },
            paths.enterprise_root / "policies.yaml": {
                "policies": spec["policies"],
            },
        }
        for path, fragment in fragments.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(fragment, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        return paths

    def write_alternate_manifest(self) -> Path:
        from data_agent.runtime.composition import canonical_json, stable_digest

        document = json.loads(self.paths().bundle_manifest.read_text(encoding="utf-8"))
        bundle = document["bundle"]
        bundle.pop("digest")
        bundle["runtime_version"] = "1.0.1"
        bundle["digest"] = stable_digest(bundle)
        document["bundleDigest"] = bundle["digest"]
        self.alternate_manifest.write_text(canonical_json(document) + "\n", encoding="utf-8")
        return self.alternate_manifest

    def test_load_verify_snapshot_and_atomic_activate(self) -> None:
        from data_agent.runtime.bundle_store import (
            BundleSourceId,
            BundleStore,
            SourceAttestation,
            VerifiedBundleCandidate,
        )

        store = BundleStore()
        candidate = store.load(self.paths())
        self.assertIsInstance(candidate, VerifiedBundleCandidate)
        self.assertTrue(store.verify(candidate))
        active = store.activate(candidate)
        self.assertEqual(active.generation, 1)
        self.assertIs(store.snapshot(), active)
        self.assertEqual(
            {item.source_id for item in active.attestations.sources},
            {
                BundleSourceId.DOMAIN_PACK,
                BundleSourceId.DOMAIN_SEMANTIC_MODEL,
                BundleSourceId.DOMAIN_METRICS,
                BundleSourceId.DOMAIN_VOCABULARY_ZH_CN,
                BundleSourceId.DOMAIN_POLICIES,
                BundleSourceId.DOMAIN_EVALS,
                BundleSourceId.ENTERPRISE_PACK,
                BundleSourceId.ENTERPRISE_PACK_LOCK,
                BundleSourceId.DEPLOYMENT_PROFILE,
                BundleSourceId.SCHEMA_CATALOG,
                BundleSourceId.BUNDLE_MANIFEST,
            },
        )
        self.assertTrue(
            all(
                isinstance(item, SourceAttestation) and len(item.sha256) == 64
                for item in active.attestations.sources
            )
        )
        self.assertNotIn(str(ROOT.resolve()), repr(active.attestations))
        with self.assertRaises(ValueError):
            store.activate(candidate)

    def test_hot_activation_never_mutates_a_pinned_snapshot(self) -> None:
        from data_agent.runtime.bundle_store import BundleStore

        store = BundleStore()
        first = store.load_and_activate(self.paths())
        pinned = store.snapshot()
        second = store.activate(store.stage(self.paths(self.write_alternate_manifest())))

        self.assertEqual(second.generation, 2)
        self.assertNotEqual(second.bundle.digest, pinned.bundle.digest)
        self.assertIs(store.snapshot(), second)
        self.assertEqual(pinned.generation, 1)
        self.assertEqual(pinned.bundle.runtime_version, "1.0.0")

    def test_activation_candidate_is_store_scoped_one_shot_and_source_attested(self) -> None:
        from data_agent.runtime.bundle_store import BundleStore, VerifiedBundleCandidate

        store = BundleStore()
        active = store.load_and_activate(self.paths())
        candidate = store.stage(self.paths(self.write_alternate_manifest()))

        with self.assertRaises((TypeError, ValueError)):
            BundleStore(active)
        with self.assertRaises((TypeError, ValueError)):
            BundleStore().activate(candidate)
        forged = object.__new__(VerifiedBundleCandidate)
        with self.assertRaises((TypeError, ValueError)):
            store.activate(forged)

        document = json.loads(self.alternate_manifest.read_text(encoding="utf-8"))
        document["bundleDigest"] = "0" * 64
        self.alternate_manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.activate(candidate)
        self.assertIs(store.snapshot(), active)

        self.write_alternate_manifest()
        with self.assertRaises(ValueError):
            store.activate(candidate)

    def test_domain_source_byte_drift_consumes_candidate_without_swap(self) -> None:
        from data_agent.runtime.bundle_store import BundleStore

        store = BundleStore()
        active = store.load_and_activate(self.paths())
        paths = self.isolated_paths()
        candidate = store.stage(paths)
        with (paths.domain_root / "metrics.yaml").open("a", encoding="utf-8") as stream:
            stream.write("\n# source drift\n")

        with self.assertRaises(ValueError):
            store.activate(candidate)
        self.assertIs(store.snapshot(), active)
        with self.assertRaises(ValueError):
            store.activate(candidate)

    def test_enterprise_source_byte_drift_consumes_candidate_without_swap(self) -> None:
        from data_agent.runtime.bundle_store import BundleStore

        store = BundleStore()
        active = store.load_and_activate(self.paths())
        paths = self.isolated_paths()
        candidate = store.stage(paths)
        with (paths.enterprise_root / "pack.yaml").open("a", encoding="utf-8") as stream:
            stream.write("\n# source drift\n")

        with self.assertRaises(ValueError):
            store.activate(candidate)
        self.assertIs(store.snapshot(), active)

    def test_deployment_source_byte_drift_consumes_candidate_without_swap(self) -> None:
        from data_agent.runtime.bundle_store import BundleStore

        store = BundleStore()
        active = store.load_and_activate(self.paths())
        paths = self.isolated_paths()
        candidate = store.stage(paths)
        with paths.deployment_profile.open("a", encoding="utf-8") as stream:
            stream.write("\n# source drift\n")

        with self.assertRaises(ValueError):
            store.activate(candidate)
        self.assertIs(store.snapshot(), active)

    def test_split_enterprise_loads_same_semantics_and_attests_actual_sources(self) -> None:
        from data_agent.runtime.bundle_store import BundleSourceId, BundleStore
        from data_agent.runtime.composition import stable_digest
        from data_agent.runtime.profile_loader import (
            enterprise_binding_source_paths,
            load_enterprise_binding,
        )

        monolithic = load_enterprise_binding(self.paths().enterprise_root)
        paths = self.split_paths()
        unused = paths.enterprise_root / "unused.yaml"
        unused.write_text("ignored: true\n", encoding="utf-8")

        actual_sources = enterprise_binding_source_paths(paths.enterprise_root)
        self.assertEqual(
            tuple(path.relative_to(paths.enterprise_root).as_posix() for path in actual_sources),
            (
                "pack.yaml",
                "sources.yaml",
                "bindings/commerce.yaml",
                "policies.yaml",
            ),
        )
        split = load_enterprise_binding(paths.enterprise_root)
        self.assertEqual(split, monolithic)
        self.assertEqual(stable_digest(split), stable_digest(monolithic))

        store = BundleStore()
        active = store.load_and_activate(paths)
        self.assertEqual(active.enterprise_binding, monolithic)
        self.assertEqual(
            {
                item.source_id
                for item in active.attestations.sources
                if item.source_id.value.startswith("enterprise/")
            },
            {
                BundleSourceId.ENTERPRISE_PACK,
                BundleSourceId.ENTERPRISE_SOURCES,
                BundleSourceId.ENTERPRISE_COMMERCE_BINDING,
                BundleSourceId.ENTERPRISE_POLICIES,
                BundleSourceId.ENTERPRISE_PACK_LOCK,
            },
        )

    def test_split_enterprise_fragments_require_every_exact_section(self) -> None:
        from data_agent.runtime.profile_loader import PackLoadError, load_enterprise_binding

        cases = (
            (Path("sources.yaml"), "domains"),
            (Path("sources.yaml"), "sources"),
            (Path("bindings") / "commerce.yaml", "bindings"),
            (Path("bindings") / "commerce.yaml", "relationships"),
            (Path("policies.yaml"), "policies"),
        )
        for relative_path, missing_key in cases:
            with self.subTest(fragment=relative_path.as_posix(), key=missing_key):
                if self.fixture_root.exists():
                    shutil.rmtree(self.fixture_root)
                paths = self.split_paths()
                fragment_path = paths.enterprise_root / relative_path
                fragment = yaml.safe_load(fragment_path.read_text(encoding="utf-8"))
                fragment.pop(missing_key)
                fragment_path.write_text(
                    yaml.safe_dump(fragment, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                with self.assertRaises(PackLoadError):
                    load_enterprise_binding(paths.enterprise_root)

    def test_each_split_enterprise_fragment_drift_rejects_without_swap(self) -> None:
        from data_agent.runtime.bundle_store import BundleStore

        store = BundleStore()
        active = store.load_and_activate(self.paths())
        fragments = (
            Path("sources.yaml"),
            Path("bindings") / "commerce.yaml",
            Path("policies.yaml"),
        )
        for relative_path in fragments:
            with self.subTest(fragment=relative_path.as_posix()):
                if self.fixture_root.exists():
                    shutil.rmtree(self.fixture_root)
                paths = self.split_paths()
                candidate = store.stage(paths)
                with (paths.enterprise_root / relative_path).open(
                    "a", encoding="utf-8"
                ) as stream:
                    stream.write("\n# source drift\n")
                with self.assertRaises(ValueError):
                    store.activate(candidate)
                self.assertIs(store.snapshot(), active)
                with self.assertRaises(ValueError):
                    store.activate(candidate)


if __name__ == "__main__":
    unittest.main()
