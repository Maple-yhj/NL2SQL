from __future__ import annotations

from typing import Any

from core.llm import LLMProtocol


CONTEXTUALIZE_SYSTEM = """
You rewrite follow-up BI questions into standalone questions for an NL2SQL engine.
Return only the rewritten question.
Use conversation context and user memories when they clarify omitted metric, time range,
dimension, filter, tenant-specific naming, or business preference.
If the current question is already standalone, return it unchanged.
Do not answer the question and do not generate SQL.
""".strip()


async def contextualize_question(
    *,
    question: str,
    conversation_history: list[dict[str, Any]],
    user_memories: list[dict[str, Any]],
    llm: LLMProtocol,
    max_output_tokens: int = 512,
) -> str:
    if not conversation_history and not user_memories:
        return question

    prompt = build_contextualization_prompt(
        question=question,
        conversation_history=conversation_history,
        user_memories=user_memories,
    )
    rewritten = await llm.complete(
        prompt=prompt,
        system=CONTEXTUALIZE_SYSTEM,
        max_output_tokens=max_output_tokens,
    )
    return rewritten.strip() or question


def build_contextualization_prompt(
    *,
    question: str,
    conversation_history: list[dict[str, Any]],
    user_memories: list[dict[str, Any]],
) -> str:
    history = "\n".join(_format_history_item(item) for item in conversation_history) or "none"
    memories = "\n".join(_format_user_memory(item) for item in user_memories) or "none"
    return f"""
[RECENT CONVERSATION]
{history}

[USER MEMORIES]
{memories}

[CURRENT QUESTION]
{question}
""".strip()


def _format_history_item(item: dict[str, Any]) -> str:
    role = str(item.get("role") or "unknown")
    content = str(item.get("content") or "").strip()
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    sql = str(metadata.get("sql") or "").strip()
    if sql:
        return f"{role}: {content}\nSQL: {sql}"
    return f"{role}: {content}"


def _format_user_memory(item: dict[str, Any]) -> str:
    key = str(item.get("memory_key") or "").strip()
    value = str(item.get("memory_value") or "").strip()
    if not key:
        return value
    return f"{key}: {value}"
