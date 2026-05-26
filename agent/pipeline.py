from __future__ import annotations

from typing import Any

from agent.react_loop import run_react_nl2sql


async def run_agent_nl2sql(
    question: str,
    tenant_id: str = "demo",
    *,
    execute: bool = False,
    llm: Any = None,
    dsn: str | None = None,
    max_limit: int = 1000,
    timeout_ms: int = 10_000,
    max_steps: int = 8,
) -> dict[str, Any]:
    """Run the public agent pipeline through the policy-controlled ReAct loop."""
    return await run_react_nl2sql(
        question,
        tenant_id,
        execute=execute,
        llm=llm,
        dsn=dsn,
        max_limit=max_limit,
        timeout_ms=timeout_ms,
        max_steps=max_steps,
    )
