from __future__ import annotations

import json
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
SMOKE_ROOT = SMOKE_PARENT / f"data-agent-task9-wheel-smoke-{uuid.uuid4().hex}"


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

        for name in ("pyproject.toml", "README.md", "schema_catalog.json"):
            shutil.copy2(ROOT / name, cls.source / name)
        shutil.copytree(ROOT / "src", cls.source / "src")
        shutil.copytree(ROOT / "packs", cls.source / "packs")
        (cls.source / "generated").mkdir()
        shutil.copytree(
            ROOT / "generated" / "bundles",
            cls.source / "generated" / "bundles",
        )
        shutil.copytree(
            ROOT / "generated" / "semantic",
            cls.source / "generated" / "semantic",
        )

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

    def test_wheel_contains_every_runtime_resource_and_no_top_level_script_dependency(self) -> None:
        self.assertEqual(self.build.returncode, 0, self.build.stderr or self.build.stdout)
        self.assertIsNotNone(self.wheel, self.build.stdout)
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
        data_prefix = "data_agent-0.1.0.data/data/share/data-agent/"
        expected = {
            *(data_prefix + "packs/domains/commerce/" + name for name in (
                "pack.yaml",
                "semantic-model.yaml",
                "metrics.yaml",
                "vocabulary.zh-CN.yaml",
                "policies.yaml",
                "evals.yaml",
            )),
            data_prefix + "packs/enterprises/olist/pack.yaml",
            data_prefix + "packs/enterprises/olist/pack.lock",
            data_prefix + "packs/deployments/olist-local.yaml",
            data_prefix + "schema_catalog.json",
            data_prefix + "generated/bundles/olist-local.json",
            data_prefix + "generated/semantic/commerce.json",
            "data_agent/runtime/maintenance.py",
            "data_agent/runtime/paths.py",
        }
        self.assertLessEqual(expected, names)
        self.assertNotIn("scripts/compile_packs.py", names)

    def test_installed_cli_works_from_non_project_cwd_without_source_imports(self) -> None:
        self.assert_setup_succeeded()
        imported = _run(
            [
                str(self.python),
                "-c",
                (
                    "import api, data_agent; "
                    "from api.app import create_app; "
                    "from data_agent.runtime.composition_root import build_olist_runtime; "
                    "from data_agent.runtime.paths import resolve_project_root; "
                    "print(api.__file__); print(data_agent.__file__); "
                    "print(create_app().openapi()['info']['title']); "
                    "print(build_olist_runtime.__name__); "
                    "print(resolve_project_root())"
                ),
            ],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertNotIn(str(self.source), imported.stdout)
        self.assertIn(str(self.venv), imported.stdout)
        self.assertIn(str(Path("share") / "data-agent"), imported.stdout)

        help_result = _run([str(self.command), "--help"], cwd=self.outside, env=self.env)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        validate = _run(
            [str(self.command), "validate-config"],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(validate.returncode, 0, validate.stderr)
        self.assertTrue(json.loads(validate.stdout)["valid"])

        compiled = self.outside / "compiled-bundle.json"
        compile_result = _run(
            [str(self.command), "compile-packs", "--output", str(compiled)],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
        self.assertTrue(compiled.is_file())

        index = self.outside / "semantic-index.json"
        rebuild_result = _run(
            [str(self.command), "rebuild-index", "--output", str(index)],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(rebuild_result.returncode, 0, rebuild_result.stderr)
        self.assertTrue(index.is_file())


if __name__ == "__main__":
    unittest.main()
