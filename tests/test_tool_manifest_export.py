import json
import unittest

from graph.tools.manifest_export import export_tool_manifest, export_tool_manifest_json
from graph.tools.registry import default_tool_registry


class ToolManifestExportTests(unittest.TestCase):
    def test_export_tool_manifest_contains_public_contract_fields(self):
        registry = default_tool_registry()

        manifest = export_tool_manifest(registry)

        self.assertEqual(manifest["version"], "1")
        self.assertEqual([tool["name"] for tool in manifest["tools"]], sorted(registry.names()))

        prepare = next(tool for tool in manifest["tools"] if tool["name"] == "prepare_sql")
        self.assertIn("sql.prepare", prepare["aliases"])
        self.assertEqual(prepare["risk_level"], "medium")
        self.assertEqual(prepare["side_effects"], "none")
        self.assertIn("properties", prepare["input_schema"])
        self.assertIn("properties", prepare["output_schema"])
        self.assertIn("examples", prepare)
        self.assertIn("response_formats", prepare)
        self.assertNotIn("handler", prepare)

    def test_every_registered_tool_has_schema_and_examples(self):
        manifest = export_tool_manifest(default_tool_registry())

        for tool in manifest["tools"]:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"])
                self.assertTrue(tool["input_schema"], tool["name"])
                self.assertTrue(tool["output_schema"], tool["name"])
                self.assertTrue(tool["examples"], tool["name"])

    def test_export_tool_manifest_json_is_deterministic_and_secret_free(self):
        registry = default_tool_registry()

        first = export_tool_manifest_json(registry)
        second = export_tool_manifest_json(registry)

        self.assertEqual(first, second)
        parsed = json.loads(first)
        self.assertEqual(parsed["version"], "1")
        self.assertNotIn("handler", first)
        self.assertNotIn("secret", first.lower())


if __name__ == "__main__":
    unittest.main()
