from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path

from api.run_streams import RunConflictError, RunCoordinator, RunEventStore
from data_agent.runtime import (
    AgentEvent,
    AgentEventType,
    AgentResponse,
    PrincipalContext,
)
from data_agent.runtime.errors import AgentError, ErrorCode
from data_agent.runtime.events import RunFailedPayload, RunStartedPayload
from data_agent.analysis_agent.models import AgentInputRequest
from data_agent.runtime.events import RunResumedPayload, RunWaitingPayload


def _started(run_id: str = "run-1") -> AgentEvent:
    return AgentEvent(
        type=AgentEventType.RUN_STARTED,
        run_id=run_id,
        sequence=0,
        data=RunStartedPayload(
            mode="execute",
            enterprise_id="olist",
            domain_id="commerce",
        ),
    )


def _cancelled(run_id: str = "run-1") -> AgentEvent:
    response = AgentResponse(
        ok=False,
        question="show revenue",
        tenant_id="tenant-a",
        error=AgentError(
            code=ErrorCode.CANCELLED,
            message="The run was cancelled.",
        ),
    )
    return AgentEvent(
        type=AgentEventType.RUN_FAILED,
        run_id=run_id,
        sequence=1,
        data=RunFailedPayload(error_code=ErrorCode.CANCELLED),
        response=response,
    )


class RunEventStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "events.sqlite3"
        self.principal = PrincipalContext(
            tenant_id="tenant-a",
            user_id="analyst-a",
        )

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_events_survive_store_recreation_and_are_principal_scoped(
        self,
    ) -> None:
        store = RunEventStore(self.path)
        await store.append(self.principal, _started())
        await store.append(self.principal, _cancelled())

        reopened = RunEventStore(self.path)
        replayed = await reopened.replay(
            tenant_id="tenant-a",
            user_id="analyst-a",
            run_id="run-1",
        )
        other_user = await reopened.replay(
            tenant_id="tenant-a",
            user_id="analyst-b",
            run_id="run-1",
        )
        other_tenant = await reopened.replay(
            tenant_id="tenant-b",
            user_id="analyst-a",
            run_id="run-1",
        )
        no_new_events = await reopened.replay(
            tenant_id="tenant-a",
            user_id="analyst-a",
            run_id="run-1",
            after_sequence=1,
        )

        self.assertEqual(
            [item.type for item in replayed],
            [AgentEventType.RUN_STARTED, AgentEventType.RUN_FAILED],
        )
        self.assertEqual(other_user, ())
        self.assertEqual(other_tenant, ())
        self.assertEqual(no_new_events, ())
        self.assertTrue(
            await reopened.contains(
                tenant_id="tenant-a",
                user_id="analyst-a",
                run_id="run-1",
            )
        )
        self.assertFalse(
            await reopened.contains(
                tenant_id="tenant-a",
                user_id="analyst-b",
                run_id="run-1",
            )
        )

    async def test_coordinator_cancels_only_the_callers_active_run(self) -> None:
        coordinator = RunCoordinator(RunEventStore(self.path))
        started = asyncio.Event()
        release = asyncio.Event()

        async def events() -> AsyncIterator[AgentEvent]:
            yield _started()
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                yield _cancelled()

        observed: list[AgentEvent] = []

        async def consume() -> None:
            async for event in coordinator.observe(
                self.principal,
                events(),
            ):
                observed.append(event)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)

        self.assertFalse(
            await coordinator.cancel(
                tenant_id="tenant-a",
                user_id="analyst-b",
                run_id="run-1",
            )
        )
        self.assertTrue(
            await coordinator.cancel(
                tenant_id="tenant-a",
                user_id="analyst-a",
                run_id="run-1",
            )
        )
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(
            [item.type for item in observed],
            [AgentEventType.RUN_STARTED, AgentEventType.RUN_FAILED],
        )
        self.assertEqual(
            observed[-1].data.error_code,
            ErrorCode.CANCELLED,
        )
        self.assertFalse(
            await coordinator.cancel(
                tenant_id="tenant-a",
                user_id="analyst-a",
                run_id="run-1",
            )
        )

    async def test_waiting_is_persisted_and_can_transition_to_resumed(self) -> None:
        store = RunEventStore(self.path)
        await store.append(self.principal, _started())
        waiting = AgentEvent(
            type=AgentEventType.RUN_WAITING,
            run_id="run-1",
            sequence=1,
            data=RunWaitingPayload(
                input_request=AgentInputRequest(
                    interrupt_id="interrupt-1",
                    reason="clarification",
                    prompt="Choose a date field",
                )
            ),
        )
        await store.append(self.principal, waiting)
        await store.append(self.principal, waiting)
        self.assertEqual(
            await store.status(
                tenant_id="tenant-a",
                user_id="analyst-a",
                run_id="run-1",
            ),
            "waiting",
        )

        resumed = AgentEvent(
            type=AgentEventType.RUN_RESUMED,
            run_id="run-1",
            sequence=2,
            data=RunResumedPayload(interrupt_id="interrupt-1"),
        )
        await store.append(self.principal, resumed)
        self.assertEqual(
            await store.status(
                tenant_id="tenant-a",
                user_id="analyst-a",
                run_id="run-1",
            ),
            "running",
        )
        replayed = await store.replay(
            tenant_id="tenant-a",
            user_id="analyst-a",
            run_id="run-1",
        )
        self.assertEqual([event.sequence for event in replayed], [0, 1, 2])

    async def test_event_sequences_are_monotonic_and_terminal_runs_are_closed(self) -> None:
        store = RunEventStore(self.path)
        await store.append(self.principal, _started())
        with self.assertRaisesRegex(ValueError, "monotonic"):
            await store.append(
                self.principal,
                AgentEvent(
                    type=AgentEventType.RUN_STARTED,
                    run_id="run-1",
                    sequence=2,
                    data=RunStartedPayload(
                        mode="execute",
                        enterprise_id="olist",
                        domain_id="commerce",
                    ),
                ),
            )
        await store.append(self.principal, _cancelled())
        with self.assertRaisesRegex(ValueError, "terminal"):
            await store.append(
                self.principal,
                AgentEvent(
                    type=AgentEventType.RUN_RESUMED,
                    run_id="run-1",
                    sequence=2,
                    data=RunResumedPayload(interrupt_id="interrupt-1"),
                ),
            )

    async def test_one_active_or_waiting_run_is_allowed_per_conversation(self) -> None:
        store = RunEventStore(self.path)
        await store.append(
            self.principal,
            _started("run-conversation-1"),
            conversation_id="conversation-1",
        )
        await store.append(
            self.principal,
            AgentEvent(
                type=AgentEventType.RUN_WAITING,
                run_id="run-conversation-1",
                sequence=1,
                data=RunWaitingPayload(
                    input_request=AgentInputRequest(
                        interrupt_id="interrupt-conversation",
                        reason="clarification",
                        prompt="Choose a date field",
                    )
                ),
            ),
        )
        with self.assertRaises(RunConflictError):
            await store.append(
                self.principal,
                _started("run-conversation-2"),
                conversation_id="conversation-1",
            )
        cancelled = _cancelled("run-conversation-1").model_copy(
            update={"sequence": 2}
        )
        await store.append(self.principal, cancelled)
        self.assertEqual(
            await store.status(
                tenant_id=self.principal.tenant_id,
                user_id=self.principal.user_id,
                run_id="run-conversation-1",
            ),
            "cancelled",
        )
        await store.append(
            self.principal,
            _started("run-conversation-2"),
            conversation_id="conversation-1",
        )
