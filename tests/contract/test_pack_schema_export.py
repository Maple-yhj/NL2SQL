from __future__ import annotations

import importlib
import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


class PackSchemaExportTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.packs = importlib.import_module("data_agent.runtime.packs")
            self.loader = importlib.import_module("data_agent.runtime.profile_loader")
        except ModuleNotFoundError as exc:
            self.fail(f"pack schema export or loader is missing: {exc}")

    def test_three_json_schemas_export_stably_and_match_repository_copies(self) -> None:
        expected_names = {
            "domain-pack.schema.json",
            "enterprise-binding.schema.json",
            "deployment-profile.schema.json",
        }
        output_dir = PROJECT_ROOT / "packs" / "schemas" / "v1"
        first_paths = self.loader.export_pack_schemas(output_dir)
        first_bytes = {path.name: path.read_bytes() for path in first_paths}
        second_paths = self.loader.export_pack_schemas(output_dir)
        second_bytes = {path.name: path.read_bytes() for path in second_paths}

        self.assertEqual(set(first_bytes), expected_names)
        self.assertEqual(first_bytes, second_bytes)
        for name, content in first_bytes.items():
            with self.subTest(schema=name):
                schema = json.loads(content)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(
                    content,
                    (PROJECT_ROOT / "packs" / "schemas" / "v1" / name).read_bytes(),
                )

    def test_yaml_loader_uses_safe_parsing_and_pydantic_validation(self) -> None:
        fixtures = PROJECT_ROOT / "tests" / "contract" / "fixtures"
        loaded = self.loader.load_pack_yaml(
            fixtures / "domain-minimal.yaml",
            self.packs.DomainPack,
        )
        self.assertEqual(loaded.metadata.name, "commerce")

        with self.assertRaises(self.loader.PackLoadError):
            self.loader.load_pack_yaml(
                fixtures / "unsafe-python-tag.yaml",
                self.packs.DomainPack,
            )


if __name__ == "__main__":
    unittest.main()
