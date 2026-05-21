import argparse
import asyncio

from engine.pipeline import run_nl2sql
from agent.pipeline import run_agent_nl2sql


async def run_once(question: str, execute: bool = False) -> None:
    result = await run_nl2sql(question, execute=execute)
    print("\n[intent]")
    print(result.intent)
    print("\n[sql]")
    print(result.sql)
    if execute:
        print("\n[rows]")
        for row in result.rows:
            print(row)

async def run_repl(execute: bool = False) -> None:
    print("NL2SQL Assistant")
    print("Type exit to quit.\n")
    while True:
        user_input = input("You> ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Bye.")
            break
        try:
            await run_once(user_input, execute=execute)
            print()
        except Exception as e:  # noqa: BLE001
            print(f"\n[ERROR] {e}\n")


async def run_once_agent(question: str, execute: bool = False) -> None:
    result = await run_agent_nl2sql(question, execute=execute)
    print("\n[intent]")
    print(result.get("intent"))
    print("\n[sql]")
    print(result.get("executed_sql"))
    if execute :
        print("\n[rows]")
        rows = result.get("rows")
        if isinstance(rows, list):
            for row in rows:
                print(row)
        explanation = result.get("explanation")
        if explanation:
            print("\n[explanation]")
            print(explanation)

async def run_repl_agent(execute: bool = False) -> None:
    print("NL2SQL Assistant Agent")
    print("Type exit to quit.\n")
    while True:
        user_input = input("You> ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Bye.")
            break
        try:
            await run_once_agent(user_input,execute=execute)
            print()
        except Exception as e:  # noqa: BLE001
            print(f"\n[ERROR] {e}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="P1 NL2SQL engine CLI")
    parser.add_argument("question", nargs="?", help="Natural language BI question.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute generated SQL against DATABASE_URL/POSTGRES_DSN.",
    )
    parser.add_argument(
    "--agent",
    action="store_true",
    help="Use P2 tool-use agent pipeline.",
    )
    args = parser.parse_args()

    if(args.agent):
        if args.question:
            asyncio.run(run_once_agent(args.question, execute=args.execute))
        else:
            asyncio.run(run_repl_agent(execute=args.execute))
    else:
        if args.question:
            asyncio.run(run_once(args.question, execute=args.execute))
        else:
            asyncio.run(run_repl(execute=args.execute))
    


if __name__ == "__main__":
    main()
