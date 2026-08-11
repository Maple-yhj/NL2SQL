from __future__ import annotations

import tempfile
import unittest

from data_agent.analysis_agent.checkpoints import (
    InMemoryCheckpointerFactory,
    SQLiteCheckpointerFactory,
)
from data_agent.analysis_agent.composition import build_analysis_runtime_from_resolver
from data_agent.analysis_agent.models import AgentStatus
from data_agent.analysis_agent.runtime import AgentResumeRequest, AnalysisRuntimeError
from data_agent.public_contracts import ErrorCode
from data_agent.runtime.events import AgentEventType
from data_agent.runtime.models import PrincipalContext
from tests.integration.test_analysis_agent_runtime import (
    PRINCIPAL,
    TestAnalysisResolver,
    analysis_request,
    clarification_decision,
)
from tests.unit.analysis_agent._graph_support import analysis_plan, finish_decision


def resume_request(interrupt_id: str = "interrupt-time-range") -> AgentResumeRequest:
    return AgentResumeRequest(
        interrupt_id=interrupt_id,
        message="Use the last 12 months",
    )


async def collect(stream):
    return [event async for event in stream]


class AnalysisAgentResumeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_does_not_persist_and_resume_persists_exactly_once(self) -> None:
        persisted: list[str] = []

        async def persist_turn(state) -> None:
            persisted.append(str(state["run_id"]))

        composition = await build_analysis_runtime_from_resolver(
            resolver=TestAnalysisResolver(
                [clarification_decision(), finish_decision(analysis_plan("pending"))],
                persist_turn=persist_turn,
            ),
            checkpointer_factory=InMemoryCheckpointerFactory(),
        )
        try:
            waiting = await collect(
                composition.runtime.run(
                    analysis_request(), PRINCIPAL, run_id="run-persist-on-resume"
                )
            )
            self.assertEqual(waiting[-1].type, AgentEventType.RUN_WAITING)
            self.assertEqual(persisted, [])
            resumed = await collect(
                composition.runtime.resume(
                    run_id="run-persist-on-resume",
                    response=resume_request(),
                    principal=PRINCIPAL,
                )
            )
            self.assertEqual(resumed[-1].type, AgentEventType.RUN_COMPLETED)
            self.assertEqual(persisted, ["run-persist-on-resume"])
            with self.assertRaises(AnalysisRuntimeError):
                await collect(
                    composition.runtime.resume(
                        run_id="run-persist-on-resume",
                        response=resume_request(),
                        principal=PRINCIPAL,
                    )
                )
            self.assertEqual(persisted, ["run-persist-on-resume"])
        finally:
            await composition.close()

    async def test_in_memory_pause_resume_and_duplicate_response_conflict(self) -> None:
        composition = await build_analysis_runtime_from_resolver(
            resolver=TestAnalysisResolver(
                [clarification_decision(), finish_decision(analysis_plan("pending"))]
            ),
            checkpointer_factory=InMemoryCheckpointerFactory(),
        )
        try:
            waiting = await collect(
                composition.runtime.run(
                    analysis_request(), PRINCIPAL, run_id="run-memory-resume"
                )
            )
            self.assertEqual(waiting[-1].type, AgentEventType.RUN_WAITING)
            resumed = await collect(
                composition.runtime.resume(
                    run_id="run-memory-resume",
                    response=resume_request(),
                    principal=PRINCIPAL,
                )
            )
            self.assertEqual(resumed[0].type, AgentEventType.RUN_RESUMED)
            self.assertEqual(resumed[-1].type, AgentEventType.RUN_COMPLETED)
            self.assertEqual(
                resumed[0].sequence,
                waiting[-1].sequence + 1,
            )
            self.assertIn(
                "last 12 months",
                resumed[-1].response.contextualized_question.lower(),
            )
            with self.assertRaises(AnalysisRuntimeError) as duplicate:
                await collect(
                    composition.runtime.resume(
                        run_id="run-memory-resume",
                        response=resume_request(),
                        principal=PRINCIPAL,
                    )
                )
            self.assertEqual(duplicate.exception.error.code, ErrorCode.AGENT_RESUME_CONFLICT)
        finally:
            await composition.close()

    async def test_sqlite_restart_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = await build_analysis_runtime_from_resolver(
                resolver=TestAnalysisResolver([clarification_decision()]),
                checkpointer_factory=SQLiteCheckpointerFactory(directory),
            )
            waiting = await collect(
                first.runtime.run(
                    analysis_request(), PRINCIPAL, run_id="run-sqlite-resume"
                )
            )
            await first.close()

            second = await build_analysis_runtime_from_resolver(
                resolver=TestAnalysisResolver([finish_decision(analysis_plan("pending"))]),
                checkpointer_factory=SQLiteCheckpointerFactory(directory),
            )
            try:
                resumed = await collect(
                    second.runtime.resume(
                        run_id="run-sqlite-resume",
                        response=resume_request(),
                        principal=PRINCIPAL,
                        start_sequence=waiting[-1].sequence + 1,
                    )
                )
                self.assertEqual(resumed[0].type, AgentEventType.RUN_RESUMED)
                self.assertEqual(resumed[-1].type, AgentEventType.RUN_COMPLETED)
                self.assertTrue(resumed[-1].response.ok)
            finally:
                await second.close()

    async def test_resume_rejects_missing_stale_interrupt_cross_owner_and_stale_pins(self) -> None:
        composition = await build_analysis_runtime_from_resolver(
            resolver=TestAnalysisResolver([clarification_decision()]),
            checkpointer_factory=InMemoryCheckpointerFactory(),
        )
        try:
            with self.assertRaises(AnalysisRuntimeError) as missing:
                await collect(
                    composition.runtime.resume(
                        run_id="missing-run",
                        response=resume_request(),
                        principal=PRINCIPAL,
                    )
                )
            self.assertEqual(
                missing.exception.error.code,
                ErrorCode.AGENT_CHECKPOINT_UNAVAILABLE,
            )

            await collect(
                composition.runtime.run(
                    analysis_request(), PRINCIPAL, run_id="run-resume-guards"
                )
            )
            with self.assertRaises(AnalysisRuntimeError) as interrupt:
                await collect(
                    composition.runtime.resume(
                        run_id="run-resume-guards",
                        response=resume_request("interrupt-old"),
                        principal=PRINCIPAL,
                    )
                )
            self.assertEqual(
                interrupt.exception.error.code,
                ErrorCode.AGENT_INTERRUPT_STALE,
            )
            for other_principal in (
                PrincipalContext(
                    tenant_id=PRINCIPAL.tenant_id,
                    user_id="different-user",
                ),
                PrincipalContext(
                    tenant_id="different-tenant",
                    user_id=PRINCIPAL.user_id,
                ),
            ):
                with self.subTest(principal=other_principal):
                    with self.assertRaises(AnalysisRuntimeError) as owner:
                        await collect(
                            composition.runtime.resume(
                                run_id="run-resume-guards",
                                response=resume_request(),
                                principal=other_principal,
                            )
                        )
                    self.assertEqual(owner.exception.error.code, ErrorCode.ACCESS_DENIED)

            composition.runtime._resolver = TestAnalysisResolver(  # type: ignore[attr-defined]
                [finish_decision(analysis_plan("pending"))],
                binding_version=4,
            )
            with self.assertRaises(AnalysisRuntimeError) as stale:
                await collect(
                    composition.runtime.resume(
                        run_id="run-resume-guards",
                        response=resume_request(),
                        principal=PRINCIPAL,
                    )
                )
            self.assertEqual(stale.exception.error.code, ErrorCode.BINDING_STALE)

            await composition.runtime._graph.compiled_graph.aupdate_state(  # type: ignore[attr-defined]
                {"configurable": {"thread_id": "run-resume-guards"}},
                {"request": {"question": ""}},
            )
            with self.assertRaises(AnalysisRuntimeError) as corrupt:
                await collect(
                    composition.runtime.resume(
                        run_id="run-resume-guards",
                        response=resume_request(),
                        principal=PRINCIPAL,
                    )
                )
            self.assertEqual(
                corrupt.exception.error.code,
                ErrorCode.AGENT_CHECKPOINT_UNAVAILABLE,
            )
        finally:
            await composition.close()

    async def test_cancelled_and_completed_runs_cannot_resume(self) -> None:
        persisted: list[dict[str, object]] = []

        async def persist_turn(state) -> None:
            persisted.append(dict(state))

        waiting = await build_analysis_runtime_from_resolver(
            resolver=TestAnalysisResolver(
                [clarification_decision()],
                persist_turn=persist_turn,
            ),
            checkpointer_factory=InMemoryCheckpointerFactory(),
        )
        try:
            await collect(
                waiting.runtime.run(
                    analysis_request(), PRINCIPAL, run_id="run-cancel-waiting"
                )
            )
            cancelled_event = await waiting.runtime.cancel_waiting(
                run_id="run-cancel-waiting",
                principal=PRINCIPAL,
                start_sequence=5,
            )
            self.assertEqual(cancelled_event.sequence, 5)
            self.assertEqual(cancelled_event.data.error_code, ErrorCode.CANCELLED)
            state = await waiting.runtime.state(
                "run-cancel-waiting",
                principal=PRINCIPAL,
            )
            self.assertEqual(AgentStatus(state["status"]), AgentStatus.CANCELLED)
            self.assertEqual(len(persisted), 1)
            self.assertEqual(
                AgentStatus(persisted[0]["status"]),
                AgentStatus.CANCELLED,
            )
            with self.assertRaises(AnalysisRuntimeError) as cancelled:
                await collect(
                    waiting.runtime.resume(
                        run_id="run-cancel-waiting",
                        response=resume_request(),
                        principal=PRINCIPAL,
                    )
                )
            self.assertEqual(
                cancelled.exception.error.code,
                ErrorCode.AGENT_RESUME_CONFLICT,
            )
        finally:
            await waiting.close()

        orphaned_persisted: list[dict[str, object]] = []

        async def persist_orphaned(state) -> None:
            orphaned_persisted.append(dict(state))

        orphaned = await build_analysis_runtime_from_resolver(
            resolver=TestAnalysisResolver(
                [clarification_decision()],
                persist_turn=persist_orphaned,
            ),
            checkpointer_factory=InMemoryCheckpointerFactory(),
        )
        try:
            await collect(
                orphaned.runtime.run(
                    analysis_request(), PRINCIPAL, run_id="run-cancel-orphaned"
                )
            )
            await orphaned.runtime._graph.compiled_graph.aupdate_state(  # type: ignore[attr-defined]
                {"configurable": {"thread_id": "run-cancel-orphaned"}},
                {"status": AgentStatus.RUNNING, "waiting_request": None},
            )
            cancelled_event = await orphaned.runtime.cancel_orphaned(
                run_id="run-cancel-orphaned",
                principal=PRINCIPAL,
                start_sequence=7,
            )
            self.assertEqual(cancelled_event.sequence, 7)
            self.assertEqual(cancelled_event.data.error_code, ErrorCode.CANCELLED)
            state = await orphaned.runtime.state(
                "run-cancel-orphaned",
                principal=PRINCIPAL,
            )
            self.assertEqual(AgentStatus(state["status"]), AgentStatus.CANCELLED)
            self.assertEqual(len(orphaned_persisted), 1)
            self.assertEqual(
                AgentStatus(orphaned_persisted[0]["status"]),
                AgentStatus.CANCELLED,
            )
        finally:
            await orphaned.close()

        completed = await build_analysis_runtime_from_resolver(
            resolver=TestAnalysisResolver([finish_decision(analysis_plan("pending"))]),
            checkpointer_factory=InMemoryCheckpointerFactory(),
        )
        try:
            events = await collect(
                completed.runtime.run(
                    analysis_request(), PRINCIPAL, run_id="run-complete-no-resume"
                )
            )
            self.assertEqual(events[-1].type, AgentEventType.RUN_COMPLETED)
            with self.assertRaises(AnalysisRuntimeError) as conflict:
                await collect(
                    completed.runtime.resume(
                        run_id="run-complete-no-resume",
                        response=resume_request(),
                        principal=PRINCIPAL,
                    )
                )
            self.assertEqual(
                conflict.exception.error.code,
                ErrorCode.AGENT_RESUME_CONFLICT,
            )
        finally:
            await completed.close()


if __name__ == "__main__":
    unittest.main()
