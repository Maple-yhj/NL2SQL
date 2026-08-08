from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKE_PARENT = Path(
    os.environ.get("DATA_AGENT_WHEEL_SMOKE_PARENT", tempfile.gettempdir())
)
SMOKE_ROOT = SMOKE_PARENT / f"data-agent-wheel-smoke-{uuid.uuid4().hex}"


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


class InstalledCliSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SMOKE_ROOT / "source"
        cls.wheels = SMOKE_ROOT / "wheels"
        cls.venv = SMOKE_ROOT / "venv"
        cls.outside = SMOKE_ROOT / "outside"
        for path in (cls.source, cls.wheels, cls.outside):
            path.mkdir(parents=True, exist_ok=True)

        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(ROOT / name, cls.source / name)
        shutil.copytree(ROOT / "src", cls.source / "src")

        cls.env = dict(os.environ)
        cls.env.pop("PYTHONPATH", None)
        cls.env.pop("DATA_AGENT_PROJECT_ROOT", None)
        cls.env.update(
            {
                "PIP_CACHE_DIR": str(SMOKE_ROOT / "pip-cache"),
            }
        )
        cls.build = _run(
            [
                sys.executable,
                "-c",
                "from setuptools import setup; setup()",
                "bdist_wheel",
                "--dist-dir",
                str(cls.wheels),
            ],
            cwd=cls.source,
            env=cls.env,
        )
        wheel_files = tuple(cls.wheels.glob("data_agent-*.whl"))
        cls.wheel = wheel_files[0] if len(wheel_files) == 1 else None

        cls.venv_create = _run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(cls.venv)],
            cwd=cls.outside,
            env=cls.env,
        )
        scripts_dir = cls.venv / ("Scripts" if os.name == "nt" else "bin")
        cls.python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
        cls.command = scripts_dir / (
            "data-agent.exe" if os.name == "nt" else "data-agent"
        )
        child_site = _run(
            [
                str(cls.python),
                "-c",
                "import site; print(site.getsitepackages()[0])",
            ],
            cwd=cls.outside,
            env=cls.env,
        )
        if child_site.returncode == 0:
            dependency_path = Path(child_site.stdout.strip()) / "parent-runtime.pth"
            dependency_path.write_text(
                sysconfig.get_paths()["purelib"],
                encoding="utf-8",
            )
        cls.install = (
            _run(
                [
                    str(cls.python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    str(cls.wheel),
                ],
                cwd=cls.outside,
                env=cls.env,
            )
            if cls.wheel is not None and cls.python.exists()
            else None
        )

    def assert_setup_succeeded(self) -> None:
        self.assertEqual(self.build.returncode, 0, self.build.stderr or self.build.stdout)
        self.assertIsNotNone(self.wheel, self.build.stdout)
        self.assertEqual(
            self.venv_create.returncode,
            0,
            self.venv_create.stderr or self.venv_create.stdout,
        )
        self.assertIsNotNone(self.install)
        self.assertEqual(
            self.install.returncode,
            0,
            self.install.stderr or self.install.stdout,
        )

    def test_wheel_contains_current_runtime_and_no_retired_pack_assets(self) -> None:
        self.assertEqual(self.build.returncode, 0, self.build.stderr or self.build.stdout)
        self.assertIsNotNone(self.wheel, self.build.stdout)
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
        expected = {
            "data_agent/analysis_agent/graph.py",
            "data_agent/analysis_agent/runtime.py",
            "data_agent/dataset_query/contracts.py",
            "data_agent/runtime/composition_root.py",
            "data_agent/tools/providers/dataset/query.py",
        }
        self.assertLessEqual(expected, names)
        retired_fragments = (
            "/packs/",
            "/generated/bundles/",
            "data_agent/execution/",
            "data_agent/skills/",
            "data_agent/runtime/maintenance.py",
            "dataset_query_service.py",
        )
        self.assertFalse(
            [name for name in names if any(item in name for item in retired_fragments)]
        )

    def test_installed_cli_works_from_non_project_cwd_without_source_imports(self) -> None:
        self.assert_setup_succeeded()
        imported = _run(
            [
                str(self.python),
                "-c",
                (
                    "import api, data_agent; "
                    "from api.app import create_app; "
                    "from data_agent.analysis_agent.graph import build_analysis_agent_graph; "
                    "from data_agent.runtime.composition_root import build_analysis_agent_runtime; "
                    "print(api.__file__); print(data_agent.__file__); "
                    "print(create_app().openapi()['info']['title']); "
                    "print(build_analysis_agent_graph.__name__); "
                    "print(build_analysis_agent_runtime.__name__)"
                ),
            ],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertNotIn(str(self.source), imported.stdout)
        self.assertIn(str(self.venv), imported.stdout)

        help_result = _run([str(self.command), "--help"], cwd=self.outside, env=self.env)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("ask", help_result.stdout)
        self.assertNotIn("compile-packs", help_result.stdout)
        self.assertNotIn("validate-config", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
