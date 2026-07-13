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
        from data_agent.runtime import load_domain_pack
        from data_agent.skills import logical_plan_from_eval_case

        domain = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        case = next(item for item in domain.spec.evals if item.id == "commerce.metric_002")
        cls.plan = logical_plan_from_eval_case(case, domain)

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
            ProposalSummary,
            RuntimeVersionPins,
        )

        pins = RuntimeVersionPins(
            bundle_digest="b" * 64,
            runtime_version="1.0.0",
            domain_pack_digest="d" * 64,
            enterprise_binding_digest="e" * 64,
            deployment_profile_digest="c" * 64,
            schema_fingerprint="f" * 64,
            skill_id="commerce.analytics",
            skill_version="1.0.0",
            graph_id="commerce.execution",
            graph_version="1.0.0",
            graph_digest="a" * 64,
            tool_registry_version="1.0.0",
            tool_versions=(ComponentVersionPin(component="semantic.search", version="1.0.0"),),
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
                    enterprise_id="olist",
                    domain_id="commerce",
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


if __name__ == "__main__":
    unittest.main()
