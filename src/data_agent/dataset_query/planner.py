"""Structured, provider-neutral logical planner for dataset queries."""

from __future__ import annotations

import json
import re
from typing import Protocol

from data_agent.datasources import SemanticBindingRecord, SemanticGraphBindingRecord
from data_agent.tools.schemas import CatalogSnapshot

from .models import (
    DatasetConversationContext,
    DatasetPlanPatch,
    DatasetPlanningResult,
    DatasetPlanStatus,
    DatasetPlanUpdate,
    DatasetQueryPlan,
)


class ModelClient(Protocol):
    model_id: str
    version: str

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str: ...


_SYSTEM_PROMPT = (
    "You are a governed dataset query planner. Return exactly one JSON object "
    "matching the supplied JSON Schema. Never return SQL or physical table/column "
    "names. Use only logical refs from logicalCatalog. If the requested metric is "
    "not defined by the catalog, return needs_clarification or unsupported instead "
    "of silently replacing it with another metric. For follow-ups, return only a "
    "DatasetPlanPatch and preserve every prior plan field not explicitly changed. "
    "Prefer a small result and never exceed the schema limit."
)
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)


class DatasetLogicalPlanner:
    def __init__(self, model_client: ModelClient, *, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._model_client = model_client
        self._max_attempts = max_attempts

    async def build_plan(
        self,
        *,
        question: str,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
        conversation_context: DatasetConversationContext | None = None,
    ) -> DatasetPlanningResult:
        type_by_physical = {
            (relation.relation, column.name): column.data_type
            for relation in catalog.relations
            for column in relation.columns
        }
        if isinstance(binding, SemanticBindingRecord):
            logical_catalog = [
                {
                    "ref": mapping.logical_ref,
                    "type": type_by_physical.get(
                        (mapping.physical_relation, mapping.physical_column),
                        "unknown",
                    ),
                }
                for mapping in binding.mappings
            ]
        else:
            relation_by_node = {
                node.node_id: node.relation_id for node in binding.graph.nodes
            }
            relation_by_id = {
                relation.relation_id: relation.relation for relation in catalog.relations
            }
            column_by_id = {
                column.column_id: (relation.relation, column.name)
                for relation in catalog.relations
                for column in relation.columns
            }
            logical_catalog = [
                {
                    "ref": mapping.logical_ref,
                    "type": type_by_physical.get(
                        column_by_id.get(mapping.column_id, ("", "")),
                        "unknown",
                    ),
                }
                for mapping in binding.mappings
                if relation_by_node.get(mapping.node_id) in relation_by_id
            ]
        if conversation_context is None:
            request = {
                "task": "create_dataset_query_plan",
                "question": question,
                "logicalCatalog": logical_catalog,
                "datasetQueryPlanSchema": DatasetQueryPlan.model_json_schema(),
            }
            contextualized_question = question
        else:
            request = {
                "task": "update_dataset_query_plan",
                "priorQuestion": conversation_context.prior_question,
                "priorPlan": conversation_context.prior_plan.model_dump(mode="json"),
                "followUpQuestion": question,
                "logicalCatalog": logical_catalog,
                "datasetPlanUpdateSchema": DatasetPlanUpdate.model_json_schema(),
                "instructions": (
                    "Choose mode=patch only for an elliptical or narrowing "
                    "follow-up; omitted patch fields inherit from priorPlan and "
                    "add_filters narrows without replacing aggregation/grouping. "
                    "Choose mode=replace for an independent new question."
                ),
            }
            contextualized_question = question
        prompt = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        failure = ""
        for attempt in range(self._max_attempts):
            raw = await self._model_client.complete(
                prompt,
                system=_SYSTEM_PROMPT,
                max_output_tokens=2048,
            )
            try:
                document = self._json_object(raw)
                plan, inherited = self._parse_plan(
                    document,
                    conversation_context=conversation_context,
                )
                if conversation_context is not None and inherited:
                    contextualized_question = (
                        f"{conversation_context.prior_question}；追问：{question}"
                    )
                plan = self._enforce_answerability(
                    question=question,
                    plan=plan,
                    logical_refs=tuple(item["ref"] for item in logical_catalog),
                )
                return DatasetPlanningResult(
                    plan=plan,
                    contextualized_question=contextualized_question,
                )
            except ValueError as exc:
                failure = str(exc)
                if attempt + 1 >= self._max_attempts:
                    break
                prompt = json.dumps(
                    {
                        "task": "repair_dataset_query_plan",
                        "input": request,
                        "previousResponse": raw[:4000],
                        "validationErrors": failure[:4000],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
        raise ValueError("model failed to produce a valid DatasetQueryPlan: " + failure)

    @staticmethod
    def _parse_plan(
        document: dict[str, object],
        *,
        conversation_context: DatasetConversationContext | None,
    ) -> tuple[DatasetQueryPlan, bool]:
        if conversation_context is None:
            return DatasetQueryPlan.model_validate(document), False
        if document.get("mode") in {"patch", "replace"}:
            update = DatasetPlanUpdate.model_validate(document)
            if update.mode == "replace":
                assert update.plan is not None
                return update.plan, False
            assert update.patch is not None
            return update.patch.apply(conversation_context.prior_plan), True
        if "analysis_type" in document and any(
            key in document for key in ("select", "aggregations", "group_by")
        ):
            return DatasetQueryPlan.model_validate(document), False
        return (
            DatasetPlanPatch.model_validate(document).apply(
                conversation_context.prior_plan
            ),
            True,
        )

    @staticmethod
    def _enforce_answerability(
        *,
        question: str,
        plan: DatasetQueryPlan,
        logical_refs: tuple[str, ...],
    ) -> DatasetQueryPlan:
        if plan.status != DatasetPlanStatus.READY:
            return plan
        lowered_refs = tuple(item.casefold() for item in logical_refs)
        refund_intent = re.search(
            r"(退款|退货|refund|returned|return rate)",
            question,
            flags=re.IGNORECASE,
        )
        refund_semantics = any(
            re.search(r"(退款|退货|refund|return)", item) for item in lowered_refs
        )
        if refund_intent is not None and not refund_semantics:
            return DatasetQueryPlan(
                status=DatasetPlanStatus.NEEDS_CLARIFICATION,
                clarification_question=(
                    "当前数据集没有可识别的退款或退货字段，无法可靠计算退款率。"
                    "请提供退款判定字段或退款事件数据。"
                ),
            )
        rate_intent = re.search(
            r"(率|占比|比例|rate|ratio|percentage|percent)",
            question,
            flags=re.IGNORECASE,
        )
        rate_semantics = any(
            re.search(r"(率|占比|比例|rate|ratio|percentage|percent)", item)
            for item in lowered_refs
        )
        if rate_intent is not None and not rate_semantics:
            return DatasetQueryPlan(
                status=DatasetPlanStatus.NEEDS_CLARIFICATION,
                clarification_question=(
                    "该问题需要派生比率，但当前语义绑定没有已定义的比率指标。"
                    "请明确分子、分母及统计口径后再查询。"
                ),
            )
        return plan

    @staticmethod
    def _json_object(value: str) -> dict[str, object]:
        text = value.strip()
        match = _FENCED_JSON.fullmatch(text)
        if match is not None:
            text = match.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < start:
                raise ValueError("model did not return a JSON object")
            text = text[start : end + 1]
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError("dataset query plan must be a JSON object")
        return document


__all__ = ["DatasetLogicalPlanner", "ModelClient"]
