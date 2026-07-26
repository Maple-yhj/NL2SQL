"""Model-backed logical planner that accepts only the typed logical schema."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from data_agent.execution import ExecutionContext, ResolvedContext
from data_agent.skills import LogicalQueryPlan
from data_agent.tools.providers import SemanticMatch

from .dependencies import ModelClient


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)
_PLANNER_SYSTEM_PROMPT = (
    "You are a governed analytics planner. Return exactly one JSON object that "
    "satisfies the supplied LogicalQueryPlan JSON Schema. Do not echo the input, "
    "schema, or instructions. Do not include Markdown, commentary, SQL, physical "
    "table names, column names, connectors, or credentials. Use the schema's exact "
    "camelCase property names; analysisType and resultShape are required. Use only "
    "canonical semantic refs present in canonicalSemanticMatches or "
    "canonicalSemanticCatalog. Search matches are hints, while the catalog is the "
    "authoritative fallback."
)
_MAX_PREVIOUS_RESPONSE_CHARS = 4000


class ModelLogicalPlanner:
    def __init__(self, model_client: ModelClient, *, max_attempts: int = 2) -> None:
        if max_attempts < 1 or max_attempts > 3:
            raise ValueError("planner max_attempts must be between 1 and 3")
        self._model_client = model_client
        self._max_attempts = max_attempts

    async def build_plan(
        self,
        *,
        context: ExecutionContext,
        resolved_context: ResolvedContext,
        semantic_matches: tuple[SemanticMatch, ...],
    ) -> LogicalQueryPlan:
        request = {
            "task": "create_logical_query_plan",
            "question": resolved_context.contextualized_question,
            "canonicalSemanticMatches": [
                {
                    "ref": item.ref,
                    "kind": item.kind.value,
                    "label": item.label,
                    "description": item.description,
                }
                for item in semantic_matches
            ],
            "canonicalSemanticCatalog": self._semantic_catalog(context),
            "approvedMemory": list(resolved_context.approved_memories),
            "conversationSummary": resolved_context.conversation_summary,
            "mode": context.mode.value,
            "logicalPlanSchema": LogicalQueryPlan.model_json_schema(),
        }
        prompt = self._initial_prompt(request)
        last_failure = ""

        for attempt in range(self._max_attempts):
            raw = await self._model_client.complete(
                prompt,
                system=_PLANNER_SYSTEM_PROMPT,
                max_output_tokens=4096,
            )
            try:
                return LogicalQueryPlan.model_validate(self._json_object(raw))
            except ValueError as exc:
                last_failure = self._failure_summary(exc)
                if attempt + 1 >= self._max_attempts:
                    break
                prompt = self._repair_prompt(
                    request=request,
                    previous_response=raw,
                    validation_failure=last_failure,
                )

        raise ValueError(
            "model failed to produce a valid LogicalQueryPlan after "
            f"{self._max_attempts} attempt(s): {last_failure}"
        )

    @staticmethod
    def _initial_prompt(request: dict[str, Any]) -> str:
        return json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _repair_prompt(
        *,
        request: dict[str, Any],
        previous_response: str,
        validation_failure: str,
    ) -> str:
        repair_request = {
            "task": "repair_logical_query_plan",
            "input": request,
            "previousResponse": previous_response[:_MAX_PREVIOUS_RESPONSE_CHARS],
            "validationErrors": validation_failure,
        }
        return (
            "The previous response was not a valid LogicalQueryPlan. Correct it using "
            "the validation errors below. Return only the corrected plan object; do "
            "not return this repair request.\n"
            "REPAIR_JSON:\n"
            + json.dumps(
                repair_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _semantic_catalog(context: ExecutionContext) -> dict[str, Any]:
        semantic_model = context.bundle.semantic_model
        return {
            key: ModelLogicalPlanner._plain_json(semantic_model[key])
            for key in ("entities", "metrics", "relationships", "policies")
            if key in semantic_model
        }

    @staticmethod
    def _plain_json(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): ModelLogicalPlanner._plain_json(item)
                for key, item in value.items()
            }
        if isinstance(value, (tuple, list)):
            return [ModelLogicalPlanner._plain_json(item) for item in value]
        return value

    @staticmethod
    def _failure_summary(exc: ValueError) -> str:
        if isinstance(exc, ValidationError):
            errors = [
                {
                    "path": ".".join(str(item) for item in error["loc"]),
                    "message": error["msg"],
                    "type": error["type"],
                }
                for error in exc.errors(include_input=False, include_url=False)
            ]
            return json.dumps(
                errors,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return str(exc)

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
