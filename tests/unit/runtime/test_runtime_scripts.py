from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC_ROOT))


class RuntimeScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output_dir = ROOT / "generated" / "bundles"
        self.bundle_output = self.output_dir / ".task8-compiled-test.json"
        self.bad_output = self.output_dir / ".task8-atomic-test.json"
        self.index_output = self.output_dir / ".task8-semantic-index-test.json"
        for path in (self.bundle_output, self.bad_output, self.index_output):
            if path.exists():
                path.unlink()

    def tearDown(self) -> None:
        for path in (self.bundle_output, self.bad_output, self.index_output):
            if path.exists():
                path.unlink()

    def test_compile_packs_is_deterministic_verified_and_atomic(self) -> None:
        compile_packs = importlib.import_module("scripts.compile_packs")

        first = compile_packs.compile_packs(
            project_root=ROOT,
            output_path=self.bundle_output,
        ).read_bytes()
        second = compile_packs.compile_packs(
            project_root=ROOT,
            output_path=self.bundle_output,
        ).read_bytes()

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            (ROOT / "generated" / "bundles" / "olist-local.json").read_bytes(),
        )
        document = json.loads(first)
        self.assertEqual(document["bundleDigest"], document["bundle"]["digest"])

        sentinel = b"previous-good-artifact\n"
        self.bad_output.write_bytes(sentinel)
        with self.assertRaises(ValueError):
            compile_packs.compile_packs(
                project_root=ROOT,
                output_path=self.bad_output,
                schema_catalog=ROOT / "packs" / "domains" / "commerce" / "pack.yaml",
            )
        self.assertEqual(self.bad_output.read_bytes(), sentinel)
        self.assertEqual(
            list(self.output_dir.glob(f".{self.bad_output.name}.*.tmp")),
            [],
        )

    def test_semantic_index_is_canonical_only_and_byte_stable(self) -> None:
        rebuild = importlib.import_module("scripts.rebuild_semantic_index")

        first = rebuild.rebuild_semantic_index(
            project_root=ROOT,
            output_path=self.index_output,
        ).read_bytes()
        second = rebuild.rebuild_semantic_index(
            project_root=ROOT,
            output_path=self.index_output,
        ).read_bytes()

        self.assertEqual(first, second)
        document = json.loads(first)
        self.assertEqual(document["kind"], "CanonicalSemanticIndex")
        self.assertEqual(document["domainPack"], "commerce@1.0.0")
        self.assertEqual(
            document["entries"],
            sorted(
                document["entries"],
                key=lambda item: (item["kind"], item["ref"]),
            ),
        )
        refs = {item["ref"] for item in document["entries"]}
        self.assertIn("commerce.gmv", refs)
        self.assertIn("commerce.Order.order_id", refs)
        serialized = first.decode("utf-8").casefold()
        for forbidden in (
            "olist_",
            "public.",
            "secret://",
            "postgresql://",
            "connectionref",
            "physical_bindings",
            "relationallowlist",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_compile_and_rebuild_accept_source_root_without_published_bundle(self) -> None:
        maintenance = importlib.import_module("data_agent.runtime.maintenance")
        source_root = ROOT / "generated" / "runtime-source-tests" / uuid4().hex
        shutil.copytree(ROOT / "packs", source_root / "packs")
        shutil.copy2(ROOT / "schema_catalog.json", source_root / "schema_catalog.json")
        try:
            self.assertFalse(
                (source_root / "generated" / "bundles" / "olist-local.json").exists()
            )

            bundle = maintenance.compile_packs(project_root=source_root)
            index = maintenance.rebuild_semantic_index(project_root=source_root)

            self.assertEqual(
                bundle,
                source_root / "generated" / "bundles" / "olist-local.json",
            )
            self.assertEqual(
                index,
                source_root / "generated" / "semantic" / "commerce.json",
            )
            self.assertTrue(bundle.is_file())
            self.assertTrue(index.is_file())
        finally:
            shutil.rmtree(source_root)

    def test_runtime_package_reexports_task_eight_contracts(self) -> None:
        runtime = importlib.import_module("data_agent.runtime")
        expected = {
            "BundleAttestations",
            "BundlePaths",
            "BundleSnapshot",
            "BundleStore",
            "BundleSourceId",
            "ContextAssembler",
            "ContextEnvelope",
            "ContextItem",
            "ContextSource",
            "ContextVersionPins",
            "DefaultDataAgentRuntime",
            "LogicalPlanner",
            "ModelClient",
            "RuntimeComposition",
            "RuntimeDependencies",
            "RuntimeVersionPins",
            "SecurityContext",
            "SourceAttestation",
            "VerifiedBundleCandidate",
            "build_olist_runtime",
            "build_upload_runtime",
        }
        self.assertLessEqual(expected, set(runtime.__all__))

    def test_scripts_are_directly_executable_without_an_editable_install(self) -> None:
        for name in ("compile_packs.py", "rebuild_semantic_index.py"):
            with self.subTest(script=name):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / name), "--help"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
