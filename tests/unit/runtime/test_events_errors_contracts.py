from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


SRC_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(SRC_ROOT))


class RuntimeEventErrorContractTests(unittest.TestCase):
    def _module(self, name: str):
        try:
            return importlib.import_module(f"data_agent.runtime.{name}")
        except ModuleNotFoundError as exc:
            self.fail(f"runtime {name} contract is missing: {exc}")

    def test_errors_events_and_runtime_protocol_are_stable(self) -> None:
        errors = self._module("errors")
        events = self._module("events")
        contracts = self._module("contracts")
        models = self._module("models")

        expected_error_codes = {
            "LOGICAL_PLAN_INVALID",
            "BINDING_STALE",
            "SQL_COMPILE_ERROR",
            "SQL_POLICY_VIOLATION",
            "COST_EXCEEDED",
            "EMPTY_RESULT",
            "JOIN_EXPLOSION",
            "ACCESS_DENIED",
            "RESULT_SEMANTIC_MISMATCH",
        }
        self.assertLessEqual(
            expected_error_codes,
            {code.value for code in errors.ErrorCode},
        )

        error = errors.AgentError(
            code=errors.ErrorCode.ACCESS_DENIED,
            message="Access denied",
            retryable=False,
        )
        self.assertEqual(error.model_dump(mode="json")["code"], "ACCESS_DENIED")
        response = models.AgentResponse(
            ok=False,
            question="restricted query",
            error=error,
        )
        self.assertEqual(response.error.code, errors.ErrorCode.ACCESS_DENIED)
        with self.assertRaises(ValidationError):
            errors.AgentError(
                code=errors.ErrorCode.ACCESS_DENIED,
                message="Access denied",
                internal_exception="must not leak",
            )

        event = events.AgentEvent(
            type=events.AgentEventType.RUN_STARTED,
            run_id="run-1",
            sequence=0,
            data={"mode": "plan"},
        )
        self.assertEqual(event.model_dump(mode="json")["type"], "run_started")

        class ExampleRuntime:
            async def run(self, request, principal):
                yield event

        self.assertIsInstance(ExampleRuntime(), contracts.DataAgentRuntime)


if __name__ == "__main__":
    unittest.main()
