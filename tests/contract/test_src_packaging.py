from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SrcPackagingContractTests(unittest.TestCase):
    def test_pyproject_discovers_data_agent_from_src_with_direct_dependencies(self) -> None:
        document = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        dependencies = document["project"]["dependencies"]
        normalized = {dependency.lower() for dependency in dependencies}
        self.assertTrue(any(item.startswith("pydantic>=2") for item in normalized))
        self.assertTrue(any(item.startswith("pyyaml>=") for item in normalized))

        setuptools = document["tool"]["setuptools"]
        self.assertEqual(setuptools["package-dir"], {"": "src"})
        self.assertEqual(setuptools["packages"]["find"]["where"], ["src"])
        self.assertIn("data_agent*", setuptools["packages"]["find"]["include"])
        self.assertNotIn("scripts", document["project"])

        dev_dependencies = {
            dependency.lower()
            for dependency in document["project"]["optional-dependencies"]["dev"]
        }
        self.assertTrue(
            any(item.startswith("jsonschema>=") for item in dev_dependencies)
        )


if __name__ == "__main__":
    unittest.main()
