"""Installable command-line adapter for the Data Agent runtime."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from data_agent.runtime import (
    AgentMode,
    AgentRequest,
    AgentResponse,
    PrincipalContext,
)


RuntimeFactory = Callable[[], Awaitable[Any]]


async def _default_runtime_factory() -> Any:
    from api.app import _default_runtime_factory as build_default_product

    return await build_default_product()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-agent",
        description="Governed Data Agent product commands",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ask = commands.add_parser("ask", help="Run one governed Data Agent question")
    ask.add_argument("question", help="Natural-language analytics question")
    ask.add_argument("--enterprise-id", default="user-dataset")
    ask.add_argument("--domain-id", default="dataset")
    ask.add_argument("--conversation-id")
    ask.add_argument("--source-id")
    ask.add_argument("--source-version", type=int)
    ask.add_argument("--binding-id")
    ask.add_argument("--binding-version", type=int)
    ask.add_argument("--mode", choices=tuple(mode.value for mode in AgentMode), default="execute")
    ask.add_argument("--requested-output", default="answer")
    ask.add_argument("--include-trace", action="store_true")
    ask.add_argument("--tenant-id", default="demo")
    ask.add_argument("--user-id", default="cli-user")
    ask.add_argument("--role", action="append", default=[])

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
            source_id=arguments.source_id,
            source_version=arguments.source_version,
            binding_id=arguments.binding_id,
            binding_version=arguments.binding_version,
            mode=arguments.mode,
            requested_output=arguments.requested_output,
            include_trace=arguments.include_trace,
        )
        principal = PrincipalContext(
            tenant_id=arguments.tenant_id,
            user_id=arguments.user_id,
            roles=tuple(arguments.role),
        )
        runtime = getattr(composition, "analysis_runtime", None)
        if runtime is None:
            runtime = composition.runtime
        terminal: AgentResponse | None = None
        waiting = None
        async for event in runtime.run(request, principal):
            if event.type.value == "run_waiting":
                waiting = event
            if event.response is None:
                continue
            if terminal is not None:
                raise RuntimeError("runtime emitted more than one terminal response")
            terminal = event.response
        if terminal is None and waiting is not None:
            print(waiting.model_dump_json(indent=2))
            return 2
        if terminal is None:
            raise RuntimeError("runtime stream ended without a terminal response")
        print(terminal.model_dump_json(indent=2))
        return 0 if terminal.ok else 1
    finally:
        await composition.close()


if __name__ == "__main__":
    raise SystemExit(main())
