from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


class TypedPublicEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from data_agent.dataset_query import AnalysisType, LogicalQueryPlan, ResultShape

        cls.plan = LogicalQueryPlan(
            analysis_type=AnalysisType.RANKING,
            metrics=("dataset.gmv",),
            dimensions=("dataset.seller_id",),
            limit=10,
            result_shape=ResultShape.RANKING,
        )

    def test_response_and_event_are_exact_typed_and_json_serializable(self) -> None:
        from data_agent.runtime.events import (
            AgentEvent,
            AgentEventType,
            RunCompletedPayload,
            RunProgressPayload,
            RunStartedPayload,
        )
        from data_agent.runtime.models import (
            AgentResponse,
            AgentRow,
            AgentTraceEntry,
            ComponentVersionPin,
            DatasetRuntimeVersionPins,
            ProposalSummary,
        )

        pins = DatasetRuntimeVersionPins(
            runtime_version="2.0.0",
            source_id="sales",
            source_version=1,
            binding_id="sales-binding",
            binding_version=1,
            schema_fingerprint="f" * 64,
            graph_id="dataset.analysis-agent",
            graph_version="1.0.0",
            graph_digest="a" * 64,
            tool_registry_version="dataset-v1",
            model_versions=(ComponentVersionPin(component="planner", version="model-v1"),),
        )
        response = AgentResponse(
            ok=True,
            question="GMV by seller",
            logical_plan=self.plan,
            rows=(AgentRow({"seller": "s-1", "gmv": 42.0, "meta": [True, None]}),),
            answer="s-1 leads",
            trace=(AgentTraceEntry(node="finalize", status="succeeded"),),
            pending_memory_updates=(
                ProposalSummary(
                    scope="enterprise",
                    source="runtime.finalize",
                    status="pending_approval",
                ),
            ),
        )
        events = (
            AgentEvent(
                type=AgentEventType.RUN_STARTED,
                run_id="run-1",
                sequence=0,
                data=RunStartedPayload(
                    mode="execute",
                    enterprise_id="user-dataset",
                    domain_id="dataset.sales",
                ),
            ),
            AgentEvent(
                type=AgentEventType.PROGRESS,
                run_id="run-1",
                sequence=1,
                data=RunProgressPayload(stage="versions_pinned", pins=pins),
            ),
            AgentEvent(
                type=AgentEventType.RUN_COMPLETED,
                run_id="run-1",
                sequence=2,
                data=RunCompletedPayload(),
                response=response,
            ),
        )

        for event in events:
            json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        terminal = events[-1].model_dump(mode="json")
        self.assertNotIn("response", terminal["data"])
        self.assertEqual(terminal["response"]["logical_plan"], self.plan.model_dump(mode="json"))
        self.assertEqual(terminal["response"]["rows"][0]["gmv"], 42.0)

    def test_type_payload_mismatch_and_arbitrary_payload_fail_closed(self) -> None:
        from data_agent.runtime.events import (
            AgentEvent,
            AgentEventType,
            RunCompletedPayload,
            RunStartedPayload,
        )
        from data_agent.runtime.models import AgentResponse

        response = AgentResponse(ok=True, question="valid")
        with self.assertRaises(ValidationError):
            AgentEvent(
                type=AgentEventType.RUN_COMPLETED,
                run_id="run-1",
                sequence=0,
                data=RunStartedPayload(
                    mode="execute",
                    enterprise_id="olist",
                    domain_id="commerce",
                ),
                response=response,
            )
        with self.assertRaises(ValidationError):
            AgentEvent(
                type=AgentEventType.PROGRESS,
                run_id="run-1",
                sequence=0,
                data={"kind": "progress", "stage": "versions_pinned", "arbitrary": object()},
            )
        with self.assertRaises(ValidationError):
            AgentEvent.model_construct(
                type=AgentEventType.RUN_COMPLETED,
                run_id="run-1",
                sequence=0,
                data=RunCompletedPayload(),
                response=None,
            )
        with self.assertRaises(ValidationError):
            AgentResponse.model_construct(
                ok=True,
                question="valid",
                rows=({"arbitrary": object()},),
            )

    def test_dataset_version_pins_and_agent_response_summaries_are_strict(self) -> None:
        from datetime import UTC, datetime

        from data_agent.analysis_agent.models import AnalysisPlan, AnalysisStep
        from data_agent.runtime.models import (
            AgentArtifactSummary,
            AgentResponse,
            AnalysisStepSummary,
            ComponentVersionPin,
            DatasetRuntimeVersionPins,
            EvidenceSummary,
        )

        plan = AnalysisPlan(
            plan_id="plan-1",
            revision=2,
            steps=(
                AnalysisStep(
                    step_id="query",
                    objective="Query revenue",
                    status="completed",
                    expected_evidence=("revenue",),
                ),
            ),
            completion_criteria=("Revenue is evidenced",),
        )
        pins = DatasetRuntimeVersionPins(
            runtime_version="2.0.0",
            graph_id="dataset.analysis-agent",
            graph_version="1.0.0",
            graph_digest="a" * 64,
            tool_registry_version="dataset-v1",
            model_versions=(
                ComponentVersionPin(component="planner", version="deepseek-chat"),
            ),
            source_id="orders",
            source_version=1,
            binding_id="binding-1",
            binding_version=2,
            schema_fingerprint="b" * 64,
            relationship_graph_digest="c" * 64,
        )
        response = AgentResponse(
            ok=True,
            question="Revenue?",
            analysis_plan=plan,
            analysis_steps=(
                AnalysisStepSummary(
                    step_id="query",
                    objective="Query revenue",
                    status="completed",
                    tool_names=("query.execute",),
                    evidence_ids=("evidence-1",),
                ),
            ),
            artifacts=(
                AgentArtifactSummary(
                    artifact_id="artifact-1",
                    kind="query_result",
                    digest="d" * 64,
                    row_count=1,
                    sensitivity="derived",
                    created_at=datetime(2026, 8, 8, tzinfo=UTC),
                ),
            ),
            evidence=(
                EvidenceSummary(
                    evidence_id="evidence-1",
                    claim_key="revenue",
                    artifact_id="artifact-1",
                    field_refs=("revenue",),
                ),
            ),
            limitations=("Read-only analysis",),
            version_pins=pins,
        )

        dumped = response.model_dump(mode="json")
        self.assertEqual(dumped["version_pins"]["kind"], "dataset")
        self.assertEqual(dumped["analysis_plan"]["revision"], 2)
        self.assertEqual(dumped["evidence"][0]["artifact_id"], "artifact-1")
        with self.assertRaises(ValidationError):
            AgentResponse.model_validate(
                {**dumped, "limitations": ["same", "same"]}
            )

    def test_waiting_event_is_stream_closing_without_agent_response(self) -> None:
        from data_agent.analysis_agent.models import AgentInputRequest
        from data_agent.runtime.events import (
            AgentEvent,
            AgentEventType,
            RunWaitingPayload,
        )
        from data_agent.runtime.models import AgentResponse

        event = AgentEvent(
            type=AgentEventType.RUN_WAITING,
            run_id="run-1",
            sequence=4,
            data=RunWaitingPayload(
                input_request=AgentInputRequest(
                    interrupt_id="interrupt-1",
                    reason="clarification",
                    prompt="Which date field should be used?",
                ),
            ),
        )
        self.assertIsNone(event.response)
        self.assertTrue(event.is_stream_closing)

        with self.assertRaises(ValidationError):
            AgentEvent(
                type=AgentEventType.RUN_WAITING,
                run_id="run-1",
                sequence=4,
                data=RunWaitingPayload(
                    input_request=AgentInputRequest(
                        interrupt_id="interrupt-1",
                        reason="clarification",
                        prompt="Which date field should be used?",
                    ),
                ),
                response=AgentResponse(ok=True, question="invalid"),
            )

    def test_all_agent_event_payloads_are_discriminated_and_exact(self) -> None:
        from data_agent.runtime.events import AgentEvent, AgentEventType

        valid = {
            "type": "tool_started",
            "run_id": "run-1",
            "sequence": 3,
            "data": {
                "kind": "tool_started",
                "call_id": "call-1",
                "action_id": "action-1",
                "tool_name": "query.preview",
                "display_name": "Preview query",
                "safe_arguments_digest": "a" * 64,
            },
            "response": None,
        }
        event = AgentEvent.model_validate(valid)
        self.assertEqual(event.type, AgentEventType.TOOL_STARTED)
        with self.assertRaises(ValidationError):
            AgentEvent.model_validate(
                {
                    **valid,
                    "data": {**valid["data"], "raw_sql": "select secret"},
                }
            )


if __name__ == "__main__":
    unittest.main()
