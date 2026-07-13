"""Installable command-line adapter for the Data Agent runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from data_agent.runtime import (
    AgentMode,
    AgentRequest,
    AgentResponse,
    BundleStore,
    PrincipalContext,
)


RuntimeFactory = Callable[[], Awaitable[Any]]


async def _default_runtime_factory() -> Any:
    from data_agent.runtime.composition_root import build_olist_runtime

    return await build_olist_runtime()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-agent",
        description="Governed Data Agent product commands",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ask = commands.add_parser("ask", help="Run one governed Data Agent question")
    ask.add_argument("question", help="Natural-language analytics question")
    ask.add_argument("--enterprise-id", default="olist")
    ask.add_argument("--domain-id", default="commerce")
    ask.add_argument("--conversation-id")
    ask.add_argument("--mode", choices=tuple(mode.value for mode in AgentMode), default="execute")
    ask.add_argument("--requested-output", default="answer")
    ask.add_argument("--include-trace", action="store_true")
    ask.add_argument("--tenant-id", default="demo")
    ask.add_argument("--user-id", default="cli-user")
    ask.add_argument("--role", action="append", default=[])

    validate = commands.add_parser(
        "validate-config",
        help="Verify packs and the published bundle without connecting to a database",
    )
    validate.add_argument("--project-root", type=Path)

    compile_command = commands.add_parser(
        "compile-packs",
        help="Compile and atomically publish the governed Runtime bundle",
    )
    compile_command.add_argument("--project-root", type=Path)
    compile_command.add_argument("--output", type=Path)

    rebuild = commands.add_parser(
        "rebuild-index",
        aliases=["rebuild-semantic-index"],
        help="Rebuild the deterministic canonical semantic index",
    )
    rebuild.add_argument("--project-root", type=Path)
    rebuild.add_argument("--output", type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "ask":
            return asyncio.run(
                _ask(
                    arguments,
                    runtime_factory or _default_runtime_factory,
                )
            )
        if arguments.command == "validate-config":
            return _validate_config(arguments.project_root)
        if arguments.command == "compile-packs":
            return _compile_packs(arguments.project_root, arguments.output)
        if arguments.command in {"rebuild-index", "rebuild-semantic-index"}:
            return _rebuild_index(arguments.project_root, arguments.output)
        raise ValueError("unsupported command")
    except Exception:
        print("Data Agent command failed safely.", file=sys.stderr)
        return 1


async def _ask(arguments: argparse.Namespace, factory: RuntimeFactory) -> int:
    composition = await factory()
    try:
        request = AgentRequest(
            question=arguments.question,
            enterprise_id=arguments.enterprise_id,
            domain_id=arguments.domain_id,
            conversation_id=arguments.conversation_id,
            mode=arguments.mode,
            requested_output=arguments.requested_output,
            include_trace=arguments.include_trace,
        )
        principal = PrincipalContext(
            tenant_id=arguments.tenant_id,
            user_id=arguments.user_id,
            roles=tuple(arguments.role),
        )
        terminal: AgentResponse | None = None
        async for event in composition.runtime.run(request, principal):
            if event.response is None:
                continue
            if terminal is not None:
                raise RuntimeError("runtime emitted more than one terminal response")
            terminal = event.response
        if terminal is None:
            raise RuntimeError("runtime stream ended without a terminal response")
        print(terminal.model_dump_json(indent=2))
        return 0 if terminal.ok else 1
    finally:
        await composition.close()


def _validate_config(project_root: Path | None) -> int:
    from data_agent.runtime.paths import bundle_paths, resolve_project_root

    root = resolve_project_root(project_root)
    snapshot = BundleStore().load_and_activate(bundle_paths(root))
    print(
        json.dumps(
            {
                "valid": True,
                "bundle_digest": snapshot.bundle.digest,
                "runtime_version": snapshot.bundle.runtime_version,
                "domain": snapshot.domain_pack.metadata.name,
                "enterprise": snapshot.enterprise_binding.metadata.name,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _compile_packs(project_root: Path | None, output: Path | None) -> int:
    from data_agent.runtime.maintenance import compile_packs

    destination = compile_packs(
        project_root=project_root,
        output_path=output,
    )
    print(destination)
    return 0


def _rebuild_index(project_root: Path | None, output: Path | None) -> int:
    from data_agent.runtime.maintenance import rebuild_semantic_index

    destination = rebuild_semantic_index(
        project_root=project_root,
        output_path=output,
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
