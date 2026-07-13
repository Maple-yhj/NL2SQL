from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.runtime.environment import load_project_environment


class ProjectEnvironmentProviderTests(unittest.TestCase):
    def tearDown(self) -> None:
        load_project_environment.cache_clear()

    def test_loads_dotenv_from_current_project_once(self) -> None:
        load_project_environment.cache_clear()
        with mock.patch(
            "data_agent.runtime.environment.Path.cwd", return_value=ROOT
        ), mock.patch(
            "data_agent.runtime.environment.load_dotenv", return_value=True
        ) as load_dotenv:
            self.assertTrue(load_project_environment())
            self.assertTrue(load_project_environment())

        load_dotenv.assert_called_once_with(ROOT / ".env")


if __name__ == "__main__":
    unittest.main()
