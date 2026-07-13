"""Model-backed logical planner that accepts only the typed logical schema."""

from __future__ import annotations

import json
import re

from data_agent.execution import ExecutionContext, ResolvedContext
from data_agent.skills import LogicalQueryPlan
from data_agent.tools.providers import SemanticMatch

from .dependencies import ModelClient


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)


class ModelLogicalPlanner:
    def __init__(self, model_client: ModelClient) -> None:
        self._model_client = model_client

    async def build_plan(
        self,
        *,
        context: ExecutionContext,
        resolved_context: ResolvedContext,
        semantic_matches: tuple[SemanticMatch, ...],
    ) -> LogicalQueryPlan:
        prompt = json.dumps(
            {
                "question": resolved_context.contextualized_question,
                "canonical_semantic_matches": [
                    {
                        "ref": item.ref,
                        "kind": item.kind.value,
                        "label": item.label,
                    }
                    for item in semantic_matches
                ],
                "approved_memory": list(resolved_context.approved_memories),
                "conversation_summary": resolved_context.conversation_summary,
                "mode": context.mode.value,
                "logical_plan_schema": LogicalQueryPlan.model_json_schema(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = await self._model_client.complete(
            prompt,
            system=(
                "Return one JSON object satisfying LogicalQueryPlan. "
                "Use only canonical semantic refs. Never emit SQL, relations, "
                "columns, connectors, credentials, or physical identifiers."
            ),
            max_output_tokens=4096,
        )
        return LogicalQueryPlan.model_validate(self._json_object(raw))

    @staticmethod
    def _json_object(value: str) -> dict:
        text = value.strip()
        match = _FENCED_JSON.fullmatch(text)
        if match is not None:
            text = match.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                raise ValueError("model did not return a JSON object")
            text = text[start : end + 1]
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError("model logical plan must be a JSON object")
        return document


__all__ = ["ModelLogicalPlanner"]
