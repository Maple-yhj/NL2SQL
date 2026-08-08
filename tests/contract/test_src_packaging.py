from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SrcPackagingContractTests(unittest.TestCase):
    def test_readme_documents_supported_local_wheel_build_command(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("python -m build", readme)
        self.assertIn(
            "python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist",
            readme,
        )

    def test_pyproject_discovers_installable_data_agent_product(self) -> None:
        document = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        dependencies = document["project"]["dependencies"]
        normalized = {dependency.lower() for dependency in dependencies}
        self.assertTrue(any(item.startswith("pydantic>=2") for item in normalized))
        self.assertTrue(any(item.startswith("langgraph>=") for item in normalized))
        self.assertFalse(any(item.startswith("pyyaml>=") for item in normalized))
        self.assertFalse(any(item.startswith("psycopg2") for item in normalized))
        self.assertEqual(document["project"]["name"], "data-agent")

        setuptools = document["tool"]["setuptools"]
        self.assertEqual(setuptools["package-dir"], {"": "src"})
        self.assertEqual(setuptools["packages"]["find"]["where"], ["src"])
        self.assertIn("data_agent*", setuptools["packages"]["find"]["include"])
        self.assertIn("api*", setuptools["packages"]["find"]["include"])
        self.assertEqual(
            document["project"]["scripts"],
            {"data-agent": "data_agent.cli:main"},
        )

        self.assertNotIn("data-files", setuptools)

        dev_dependencies = {
            dependency.lower()
            for dependency in document["project"]["optional-dependencies"]["dev"]
        }
        self.assertFalse(any(item.startswith("jsonschema>=") for item in dev_dependencies))


if __name__ == "__main__":
    unittest.main()
