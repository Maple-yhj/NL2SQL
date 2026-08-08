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
        self.assertEqual(request.enterprise_id, "user-dataset")
        self.assertEqual(request.domain_id, "dataset")
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

    def test_datasource_pins_are_atomic(self) -> None:
        models = self._runtime_models()
        request = models.AgentRequest(
            question="show rows",
            enterprise_id="user-dataset",
            domain_id="dataset.orders",
            source_id="orders",
            source_version=1,
            binding_id="orders-binding-1",
            binding_version=2,
        )
        self.assertEqual(request.source_version, 1)

        with self.assertRaises(ValidationError):
            models.AgentRequest(
                question="show rows",
                source_id="orders",
                source_version=1,
            )

    def test_chart_can_only_reference_numeric_returned_columns(self) -> None:
        models = self._runtime_models()
        response = models.AgentResponse(
            ok=True,
            question="sales by city",
            rows=(
                models.AgentRow(root={"city": "Shanghai", "sales": 12.5}),
            ),
            chart=models.ChartSpec(
                title="Sales by city",
                x_field="city",
                y_field="sales",
            ),
        )
        self.assertEqual(response.chart.chart_type, "bar")

        with self.assertRaises(ValidationError):
            models.AgentResponse(
                ok=True,
                question="unsafe chart",
                rows=(
                    models.AgentRow(root={"city": "Shanghai", "sales": 12.5}),
                ),
                chart=models.ChartSpec(
                    title="Unsafe",
                    x_field="city",
                    y_field="missing",
                ),
            )

    def test_runtime_package_reexports_the_current_product_contract(self) -> None:
        runtime = importlib.import_module("data_agent.runtime")
        expected_exports = {
            "AgentRequest",
            "PrincipalContext",
            "RunBudget",
            "AgentEvent",
            "AgentResponse",
            "ErrorCode",
            "DataAgentRuntime",
            "DatasetRuntimeVersionPins",
            "UploadDatasetRuntime",
            "build_analysis_agent_runtime",
            "build_upload_runtime",
        }
        self.assertLessEqual(expected_exports, set(getattr(runtime, "__all__", ())))
        retired = {
            "DomainPack",
            "EnterpriseDataBinding",
            "DeploymentProfile",
            "ResolvedRuntimeBundle",
            "compile_runtime_bundle",
            "load_pack_yaml",
        }
        self.assertTrue(retired.isdisjoint(getattr(runtime, "__all__", ())))


if __name__ == "__main__":
    unittest.main()
