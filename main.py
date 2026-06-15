from __future__ import annotations

import argparse
import asyncio

from graph.pipeline import run_nl2sql


async def run_once(
    question: str,
    *,
    tenant_id: str = "demo",
    execute: bool = False,
) -> None:
    result = await run_nl2sql(
        question,
        tenant_id=tenant_id,
        execute=execute,
    )
    print("\n[intent]")
    print(result["intent"])
    print("\n[sql]")
    print(result["sql"])
    if execute:
        print("\n[rows]")
        for row in result["rows"]:
            print(row)
        print("\n[answer]")
        print(result["answer"])
    if not result["ok"]:
        print("\n[error]")
        print(result["error"])


async def run_repl(*, tenant_id: str = "demo", execute: bool = False) -> None:
    print("NL2SQL LangGraph Assistant")
    print("Type exit to quit.\n")
    while True:
        user_input = input("You> ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break
        await run_once(user_input, tenant_id=tenant_id, execute=execute)


def main() -> None:
    parser = argparse.ArgumentParser(description="LangGraph NL2SQL assistant")
    parser.add_argument("question", nargs="?", help="Natural-language BI question")
    parser.add_argument("--tenant-id", default="demo")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    if args.question:
        asyncio.run(
            run_once(
                args.question,
                tenant_id=args.tenant_id,
                execute=args.execute,
            )
        )
    else:
        asyncio.run(run_repl(tenant_id=args.tenant_id, execute=args.execute))


if __name__ == "__main__":
    main()
