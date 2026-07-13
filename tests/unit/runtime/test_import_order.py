from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class RuntimeImportOrderTests(unittest.TestCase):
    def test_execution_can_be_the_first_data_agent_import(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    f"sys.path.insert(0, {str(ROOT / 'src')!r}); "
                    "import data_agent.execution; "
                    "from data_agent.runtime import DefaultDataAgentRuntime, RuntimeDependencies; "
                    "assert DefaultDataAgentRuntime and RuntimeDependencies"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
