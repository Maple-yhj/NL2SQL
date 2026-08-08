from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "audit_repository_reachability.py"


class RepositoryReachabilityAuditTests(unittest.TestCase):
    def test_report_is_deterministic_relative_and_separates_root_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            for destination in (first, second):
                subprocess.run(
                    [sys.executable, str(SCRIPT), "--output", str(destination)],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            report = json.loads(first.read_text(encoding="utf-8"))

        self.assertEqual(set(report["roots"]), {"product", "scripts", "tests"})
        self.assertIn("src/api/app.py", report["roots"]["product"])
        self.assertIn("src/data_agent/cli.py", report["reachability"]["product"])
        self.assertIn(
            "src/data_agent/dataset_query/compiler.py",
            report["reachability"]["product"],
        )
        self.assertNotIn("src/api/dataset_query_service.py", report["reachability"]["tests"])
        self.assertFalse(
            [
                path
                for path in report["reachability"]["product"]
                if path.startswith("src/data_agent/execution/")
            ]
        )
        serialized = json.dumps(report)
        self.assertNotIn(str(ROOT), serialized)
        self.assertNotIn(".env.codex-backup", serialized)

    def test_module_can_build_report_without_writing_repository_output(self) -> None:
        spec = importlib.util.spec_from_file_location("repository_audit", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = module.build_report()
        self.assertEqual(report["schema_version"], 1)
        self.assertIn("data-agent", report["packaging"]["project_scripts"])


if __name__ == "__main__":
    unittest.main()
