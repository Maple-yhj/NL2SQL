from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


SRC_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(SRC_ROOT))


class RuntimePublicModelTests(unittest.TestCase):
    def _runtime_models(self):
        try:
            return importlib.import_module("data_agent.runtime.models")
        except ModuleNotFoundError as exc:
            self.fail(f"runtime public models are missing: {exc}")

    def test_runtime_public_models_expose_stable_product_contracts(self) -> None:
        models = self._runtime_models()

        request = models.AgentRequest(question="  monthly GMV  ")
        self.assertEqual(request.question, "monthly GMV")
        self.assertEqual(request.enterprise_id, "olist")
        self.assertEqual(request.domain_id, "commerce")
        self.assertEqual(request.mode, "execute")
        self.assertEqual(request.requested_output, "answer")
        self.assertFalse(request.include_trace)

        principal = models.PrincipalContext(
            tenant_id="seller-1",
            user_id="analyst-1",
            roles=["analyst"],
        )
        self.assertEqual(principal.roles, ("analyst",))

        budget = models.RunBudget()
        self.assertEqual(budget.max_tool_calls, 24)
        self.assertEqual(budget.max_correction_rounds, 2)
        self.assertEqual(budget.max_sql_compile_attempts, 3)
        self.assertEqual(budget.max_duration_seconds, 120)
        self.assertEqual(budget.max_result_rows, 1000)

        response_fields = {
            "ok",
            "question",
            "contextualized_question",
            "conversation_id",
            "tenant_id",
            "logical_plan",
            "sql",
            "message_type",
            "rows",
            "answer",
            "error",
            "trace",
            "pending_memory_updates",
        }
        self.assertLessEqual(response_fields, set(models.AgentResponse.model_fields))

        with self.assertRaises(ValidationError):
            models.AgentRequest(question="   ")

        with self.assertRaises(ValidationError):
            models.AgentRequest(question="valid", mode="unsafe")

    def test_runtime_package_reexports_the_public_task_one_contract(self) -> None:
        runtime = importlib.import_module("data_agent.runtime")
        expected_exports = {
            "AgentRequest",
            "PrincipalContext",
            "RunBudget",
            "AgentEvent",
            "AgentResponse",
            "ErrorCode",
            "DataAgentRuntime",
            "DomainPack",
            "EnterpriseDataBinding",
            "DeploymentProfile",
            "ResolvedRuntimeBundle",
            "compile_runtime_bundle",
            "export_pack_schemas",
            "load_pack_yaml",
        }
        self.assertLessEqual(expected_exports, set(getattr(runtime, "__all__", ())))


if __name__ == "__main__":
    unittest.main()
