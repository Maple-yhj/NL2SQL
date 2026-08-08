"""Emit a deterministic repository reachability and reference audit.

The report deliberately contains repository-relative paths only. It separates
product, maintenance-script and pytest reachability so test-only compatibility
code cannot be mistaken for a production dependency.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import tempfile
import tomllib
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
_SKIP_PARTS = frozenset(
    {
        ".git",
        ".impeccable",
        ".kaggle",
        ".pnpm-store",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
_TEXT_SUFFIXES = frozenset(
    {".json", ".md", ".mjs", ".py", ".toml", ".ts", ".tsx", ".yaml", ".yml"}
)
_PATH_LITERAL = re.compile(
    r"(?P<path>(?:\./|\.\./)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
)
_FRONTEND_IMPORT = re.compile(
    r"(?:from\s+|import\s*\()(?P<quote>['\"])(?P<value>\.[^'\"]+)(?P=quote)"
)
_JS_COMMAND = re.compile(
    r"\b(?:exec|execFile|spawn|spawnSync)\s*\(\s*['\"](?P<value>[^'\"]+)['\"]"
)


def _relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


def _is_scannable(path: Path) -> bool:
    try:
        relative = path.relative_to(PROJECT_ROOT)
    except ValueError:
        return False
    return not any(part in _SKIP_PARTS or part.startswith(".env") for part in relative.parts)


def _files(*, suffixes: frozenset[str] | None = None) -> tuple[Path, ...]:
    output = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or not _is_scannable(path):
            continue
        if suffixes is not None and path.suffix not in suffixes:
            continue
        output.append(path)
    return tuple(sorted(output, key=_relative))


def _tracked_files() -> frozenset[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return frozenset(
        item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
    )


def _module_name(path: Path) -> str | None:
    if path == PROJECT_ROOT / "main.py":
        return "main"
    for base, prefix in (
        (SOURCE_ROOT, ""),
        (PROJECT_ROOT / "tests", "tests"),
        (PROJECT_ROOT / "scripts", "scripts"),
    ):
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if prefix:
            parts.insert(0, prefix)
        return ".".join(parts)
    return None


def _resolve_module(
    name: str,
    module_files: dict[str, str],
) -> str | None:
    candidate = name
    while candidate:
        target = module_files.get(candidate)
        if target is not None:
            return target
        candidate = candidate.rpartition(".")[0]
    return None


def _absolute_import(
    current_module: str,
    node: ast.ImportFrom,
    *,
    is_package: bool,
) -> str:
    if node.level == 0:
        return node.module or ""
    package = current_module.split(".")
    if not is_package:
        package.pop()
    ascend = node.level - 1
    prefix = package[: max(0, len(package) - ascend)]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _python_graph(
    python_files: tuple[Path, ...],
) -> tuple[dict[str, set[str]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    module_files = {
        module: _relative(path)
        for path in python_files
        if (module := _module_name(path)) is not None
    }
    graph: dict[str, set[str]] = {
        _relative(path): set() for path in python_files
    }
    for module, relative in module_files.items():
        parts = module.split(".")
        for index in range(1, len(parts)):
            package_target = module_files.get(".".join(parts[:index]))
            if package_target is not None and package_target != relative:
                graph[relative].add(package_target)
    imports: list[dict[str, Any]] = []
    dynamic: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    subprocess_names = {"run", "Popen", "call", "check_call", "check_output"}

    for path in python_files:
        relative = _relative(path)
        module = _module_name(path) or ""
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"cannot parse {relative}: {exc}") from exc
        for line, imported_module in _lazy_export_modules(tree, module):
            target = _resolve_module(imported_module, module_files)
            imports.append(
                {
                    "file": relative,
                    "line": line,
                    "kind": "lazy_export",
                    "module": imported_module,
                    "target": target,
                }
            )
            dynamic.append(
                {"file": relative, "line": line, "module": imported_module}
            )
            if target is not None and target != relative:
                graph[relative].add(target)
        for node in ast.walk(tree):
            imported: list[tuple[str, str]] = []
            if isinstance(node, ast.Import):
                imported.extend((alias.name, "static") for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = _absolute_import(module, node, is_package=path.name == "__init__.py")
                imported.append((base, "static"))
                imported.extend(
                    (f"{base}.{alias.name}" if base else alias.name, "static")
                    for alias in node.names
                    if alias.name != "*"
                )
            elif isinstance(node, ast.Call):
                call_name = _call_name(node.func)
                if call_name in {"importlib.import_module", "__import__"}:
                    literal = _first_string_argument(node)
                    if literal:
                        imported.append((literal, "dynamic"))
                        dynamic.append(
                            {"file": relative, "line": node.lineno, "module": literal}
                        )
                if call_name.rpartition(".")[2] in subprocess_names:
                    literal = _first_string_argument(node)
                    if literal:
                        commands.append(
                            {"file": relative, "line": node.lineno, "command": literal}
                        )
            for imported_module, kind in imported:
                target = _resolve_module(imported_module, module_files)
                imports.append(
                    {
                        "file": relative,
                        "line": getattr(node, "lineno", 0),
                        "kind": kind,
                        "module": imported_module,
                        "target": target,
                    }
                )
                if target is not None and target != relative:
                    graph[relative].add(target)
    return graph, imports, dynamic, commands


def _lazy_export_modules(tree: ast.Module, package: str) -> tuple[tuple[int, str], ...]:
    """Resolve the repository's declarative `_LAZY_EXPORTS` import maps."""

    found: set[tuple[int, str]] = set()
    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_LAZY_EXPORTS"
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_LAZY_EXPORTS"
        ):
            value = node.value
        if not isinstance(value, ast.Dict):
            continue
        for entry in value.values:
            if not isinstance(entry, (ast.Tuple, ast.List)) or not entry.elts:
                continue
            module_node = entry.elts[0]
            if not isinstance(module_node, ast.Constant) or not isinstance(
                module_node.value, str
            ):
                continue
            imported = module_node.value
            if imported.startswith("."):
                imported = importlib.util.resolve_name(imported, package)
            found.add((module_node.lineno, imported))
    return tuple(sorted(found))


def _call_name(value: ast.expr) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        prefix = _call_name(value.value)
        return f"{prefix}.{value.attr}" if prefix else value.attr
    return ""


def _first_string_argument(node: ast.Call) -> str | None:
    if not node.args:
        return None
    value = node.args[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, (ast.List, ast.Tuple)) and value.elts:
        first = value.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _walk(graph: dict[str, set[str]], roots: Iterable[str]) -> list[str]:
    reached: set[str] = set()
    pending = deque(sorted(set(roots)))
    while pending:
        current = pending.popleft()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(sorted(graph.get(current, ())))
    return sorted(reached)


def _python_roots(
    python_files: tuple[Path, ...],
    pyproject: dict[str, Any],
) -> dict[str, list[str]]:
    available = {_relative(path) for path in python_files}
    module_files = {
        module: _relative(path)
        for path in python_files
        if (module := _module_name(path)) is not None
    }
    product = {
        item
        for item in ("main.py", "src/api/app.py")
        if item in available
    }
    for target in pyproject.get("project", {}).get("scripts", {}).values():
        module = str(target).partition(":")[0]
        if resolved := _resolve_module(module, module_files):
            product.add(resolved)
    langgraph = json.loads((PROJECT_ROOT / "langgraph.json").read_text(encoding="utf-8"))
    for target in langgraph.get("graphs", {}).values():
        path_text = str(target).partition(":")[0].removeprefix("./")
        if path_text in available:
            product.add(path_text)
    return {
        "product": sorted(product),
        "scripts": sorted(
            _relative(path)
            for path in python_files
            if path.is_relative_to(PROJECT_ROOT / "scripts")
        ),
        "tests": sorted(
            _relative(path)
            for path in python_files
            if path.is_relative_to(PROJECT_ROOT / "tests")
        ),
    }


def _resolve_literal_path(source: Path, literal: str) -> str | None:
    clean = literal.split("#", 1)[0].split("?", 1)[0]
    candidates = []
    if clean.startswith("./") or clean.startswith("../"):
        candidates.append(source.parent / clean)
    else:
        candidates.append(PROJECT_ROOT / clean)
        candidates.append(source.parent / clean)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(PROJECT_ROOT.resolve())
        except (OSError, ValueError):
            continue
        if resolved.exists():
            return _relative(resolved)
    return None


def _literal_references(text_files: tuple[Path, ...]) -> list[dict[str, Any]]:
    references: set[tuple[str, int, str, str]] = set()
    for path in text_files:
        relative = _relative(path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in _PATH_LITERAL.finditer(line):
                literal = match.group("path").rstrip(".,:;)]}")
                target = _resolve_literal_path(path, literal)
                if target is not None and target != relative:
                    references.add((relative, line_number, literal, target))
    return [
        {"file": file, "line": line, "literal": literal, "target": target}
        for file, line, literal, target in sorted(references)
    ]


def _frontend_graph() -> dict[str, Any]:
    root = PROJECT_ROOT / "frontend"
    source_files = tuple(
        path
        for path in _files(suffixes=frozenset({".js", ".jsx", ".mjs", ".ts", ".tsx"}))
        if path.is_relative_to(root)
    )
    available = {_relative(path): path for path in source_files}
    graph: dict[str, set[str]] = {name: set() for name in available}
    commands: list[dict[str, Any]] = []
    for relative, path in available.items():
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in _FRONTEND_IMPORT.finditer(line):
                target = _resolve_frontend_import(path, match.group("value"), available)
                if target:
                    graph[relative].add(target)
            for match in _JS_COMMAND.finditer(line):
                commands.append(
                    {"file": relative, "line": line_number, "command": match.group("value")}
                )
    entry = "frontend/src/main.tsx"
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    reached = _walk(graph, [entry] if entry in graph else [])
    source_names = sorted(name for name in graph if name.startswith("frontend/src/"))
    return {
        "entrypoints": ([entry] if entry in graph else []),
        "package_scripts": package.get("scripts", {}),
        "reachable": reached,
        "unreachable_source_files": sorted(set(source_names) - set(reached)),
        "subprocess_commands": sorted(commands, key=lambda item: (item["file"], item["line"])),
    }


def _resolve_frontend_import(
    source: Path,
    value: str,
    available: dict[str, Path],
) -> str | None:
    base = source.parent / value
    candidates = [base]
    candidates.extend(
        Path(f"{base}{suffix}")
        for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs")
    )
    candidates.extend(base.with_suffix(suffix) for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs"))
    candidates.extend(base / f"index{suffix}" for suffix in (".ts", ".tsx", ".js", ".jsx"))
    for candidate in candidates:
        try:
            relative = _relative(candidate)
        except (OSError, ValueError):
            continue
        if relative in available:
            return relative
    return None


def build_report() -> dict[str, Any]:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    python_files = _files(suffixes=frozenset({".py"}))
    text_files = _files(suffixes=_TEXT_SUFFIXES)
    tracked = _tracked_files()
    graph, imports, dynamic, python_commands = _python_graph(python_files)
    roots = _python_roots(python_files, pyproject)
    reachability = {name: _walk(graph, values) for name, values in roots.items()}
    source_files = sorted(name for name in graph if name.startswith("src/"))
    product = set(reachability["product"])
    tests_or_scripts = set(reachability["tests"]) | set(reachability["scripts"])
    data_files = pyproject.get("tool", {}).get("setuptools", {}).get("data-files", {})
    report = {
        "schema_version": 1,
        "roots": roots,
        "reachability": reachability,
        "summary": {
            "python_file_count": len(python_files),
            "tracked_file_count": len(tracked),
            "product_reachable_source_count": len(product.intersection(source_files)),
            "source_not_product_reachable_count": len(set(source_files) - product),
        },
        "source_not_product_reachable": sorted(set(source_files) - product),
        "source_only_test_or_script_reachable": sorted(
            (set(source_files) - product).intersection(tests_or_scripts)
        ),
        "source_unreachable_from_all_python_roots": sorted(
            set(source_files) - product - tests_or_scripts
        ),
        "python_imports": sorted(
            imports,
            key=lambda item: (item["file"], item["line"], item["kind"], item["module"]),
        ),
        "dynamic_imports": sorted(dynamic, key=lambda item: (item["file"], item["line"])),
        "subprocess_commands": sorted(
            python_commands, key=lambda item: (item["file"], item["line"])
        ),
        "literal_path_references": _literal_references(text_files),
        "packaging": {
            "project_scripts": pyproject.get("project", {}).get("scripts", {}),
            "dependencies": pyproject.get("project", {}).get("dependencies", []),
            "dev_dependencies": pyproject.get("project", {})
            .get("optional-dependencies", {})
            .get("dev", []),
            "data_files": data_files,
        },
        "frontend": _frontend_graph(),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON here instead of stdout (must be inside the repository or /tmp).",
    )
    arguments = parser.parse_args(argv)
    payload = json.dumps(build_report(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(payload, end="")
        return 0
    destination = arguments.output.resolve()
    temporary_roots = {Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}
    allowed = destination.is_relative_to(PROJECT_ROOT.resolve()) or any(
        destination.is_relative_to(root) for root in temporary_roots
    )
    if not allowed:
        raise ValueError("audit output must stay inside the repository or /tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8")
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
