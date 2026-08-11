from __future__ import annotations

import unittest

from data_agent.analysis_agent.models import AgentObservation
from data_agent.dataset_query import (
    DatasetAggregateExpression,
    DatasetFieldExpression,
    DatasetOutputExpression,
    DatasetProjection,
    DatasetQueryPlan,
    DatasetQueryProgram,
    DatasetQueryStage,
    DatasetRootSource,
    DatasetStageSource,
)
from data_agent.runtime.models import AgentMode
from data_agent.tools.models import ToolErrorCode
from data_agent.tools.providers.dataset.contracts import (
    ArtifactInput,
    ChartRenderInput,
    ComputationSpec,
    EvidenceCollectInput,
    QueryCompileInput,
    QueryRunInput,
)
from data_agent.tools.schemas import TabularResult

from ._support import DatasetToolHarness, invoke


class DatasetQueryProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = DatasetToolHarness()

    def tearDown(self) -> None:
        self.harness.close()

    async def test_multistage_program_compiles_and_executes_through_tool_contracts(
        self,
    ) -> None:
        runtime, invoker, context = self.harness.invocation(AgentMode.EXECUTE)
        program = DatasetQueryProgram(
            stages=(
                DatasetQueryStage(
                    stage_id="by_state",
                    input=DatasetRootSource(),
                    projections=(
                        DatasetProjection(
                            alias="state",
                            expression=DatasetFieldExpression(
                                ref="dataset.orders.state"
                            ),
                        ),
                        DatasetProjection(
                            alias="amount_total",
                            expression=DatasetAggregateExpression(
                                operation="sum",
                                operand=DatasetFieldExpression(
                                    ref="dataset.orders.amount"
                                ),
                            ),
                        ),
                    ),
                    group_by=(
                        DatasetFieldExpression(ref="dataset.orders.state"),
                    ),
                ),
                DatasetQueryStage(
                    stage_id="summary",
                    input=DatasetStageSource(stage_id="by_state"),
                    projections=(
                        DatasetProjection(
                            alias="state_count",
                            expression=DatasetAggregateExpression(operation="count"),
                        ),
                        DatasetProjection(
                            alias="maximum_amount",
                            expression=DatasetAggregateExpression(
                                operation="max",
                                operand=DatasetOutputExpression(
                                    stage_id="by_state", name="amount_total"
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            output_stage_id="summary",
        )
        compiled = await invoke(
            invoker,
            context,
            call_id="program-compile",
            tool_name="query.compile",
            input_data=QueryCompileInput(plan=program),
        )
        executed = await invoke(
            invoker,
            context,
            call_id="program-execute",
            tool_name="query.execute",
            input_data=QueryRunInput(
                artifact_id=compiled.typed_data.artifact.artifact_id
            ),
        )

        result = TabularResult.model_validate(
            await runtime.artifacts.get_json(
                tenant_id=self.harness.tenant_id,
                user_id=self.harness.user_id,
                run_id="run-1",
                artifact_id=executed.typed_data.artifact.artifact_id,
            )
        )
        self.assertEqual(compiled.status, "success")
        self.assertEqual(executed.status, "success")
        self.assertEqual(result.columns, ("state_count", "maximum_amount"))
        self.assertEqual(result.rows[0].values, (2, 55.0))

    async def test_query_profile_compute_chart_and_evidence_pipeline(self) -> None:
        runtime, invoker, context = self.harness.invocation(AgentMode.EXECUTE)
        plan = DatasetQueryPlan(
            analysis_type="detail",
            select=(
                "dataset.orders.state",
                "dataset.orders.amount",
                "dataset.orders.quantity",
            ),
            limit=3,
        )
        compiled = await invoke(
            invoker,
            context,
            call_id="query-compile",
            tool_name="query.compile",
            input_data=QueryCompileInput(plan=plan),
        )
        self.assertEqual(compiled.status, "success")
        prepared_id = compiled.typed_data.artifact.artifact_id

        explained = await invoke(
            invoker,
            context,
            call_id="query-explain",
            tool_name="query.explain",
            input_data=QueryRunInput(artifact_id=prepared_id),
        )
        previewed = await invoke(
            invoker,
            context,
            call_id="query-preview",
            tool_name="query.preview",
            input_data=QueryRunInput(artifact_id=prepared_id, preview_rows=2),
        )
        executed = await invoke(
            invoker,
            context,
            call_id="query-execute",
            tool_name="query.execute",
            input_data=QueryRunInput(artifact_id=prepared_id),
        )
        data_profile = await invoke(
            invoker,
            context,
            call_id="data-profile",
            tool_name="data.profile",
            input_data=QueryRunInput(artifact_id=prepared_id, preview_rows=2),
        )

        self.assertEqual(explained.status, "success")
        self.assertEqual(previewed.rows, 2)
        self.assertEqual(executed.rows, 3)
        self.assertEqual(data_profile.status, "success")

        result_id = executed.typed_data.artifact.artifact_id
        result_document = await runtime.artifacts.get_json(
            tenant_id=self.harness.tenant_id,
            user_id=self.harness.user_id,
            run_id="run-1",
            artifact_id=result_id,
        )
        result = TabularResult.model_validate(result_document)
        amount_field = result.columns[1]

        profiled = await invoke(
            invoker,
            context,
            call_id="result-profile",
            tool_name="result.profile",
            input_data=ArtifactInput(artifact_id=result_id),
        )
        computed = await invoke(
            invoker,
            context,
            call_id="analysis-compute",
            tool_name="analysis.compute",
            input_data=ComputationSpec(
                operation="describe",
                artifact_id=result_id,
                fields=(amount_field,),
            ),
        )
        charted = await invoke(
            invoker,
            context,
            call_id="chart-render",
            tool_name="chart.render",
            input_data=ChartRenderInput(
                artifact_id=result_id,
                title="Order amounts by state",
                x_field=result.columns[0],
                y_field=amount_field,
            ),
        )
        evidenced = await invoke(
            invoker,
            context,
            call_id="evidence-collect",
            tool_name="evidence.collect",
            input_data=EvidenceCollectInput(
                artifact_id=result_id,
                claim_key="order_amounts",
                field_refs=(amount_field,),
                sql_digest=compiled.typed_data.query_hash,
            ),
        )

        for derived in (profiled, computed, charted, evidenced):
            self.assertEqual(derived.status, "success")
        for index, tool_result in enumerate(
            (
                compiled,
                explained,
                previewed,
                executed,
                data_profile,
                profiled,
                computed,
                charted,
            )
        ):
            AgentObservation(
                observation_id=f"artifact-observation-{index}",
                action_id=f"artifact-action-{index}",
                tool_name=tool_result.redacted_trace.tool_name,
                status="succeeded",
                summary=tool_result.typed_data.summary,
                artifact_refs=(tool_result.typed_data.artifact,),
            )
        self.assertEqual(evidenced.typed_data.evidence.result_digest, executed.typed_data.artifact.digest)
        self.assertEqual(evidenced.redacted_trace.evidence_ids, (evidenced.typed_data.evidence.evidence_id,))
        AgentObservation(
            observation_id="evidence-observation",
            action_id="evidence-action",
            tool_name="evidence.collect",
            status="succeeded",
            summary=evidenced.typed_data.summary,
            artifact_refs=(evidenced.typed_data.artifact,),
            evidence_refs=(evidenced.typed_data.evidence,),
        )

    async def test_compile_failures_keep_a_typed_public_diagnostic(self) -> None:
        _, invoker, context = self.harness.invocation(AgentMode.EXECUTE)
        invalid = DatasetQueryPlan(
            analysis_type="detail",
            select=("dataset.orders.unknown_field",),
        )

        result = await invoke(
            invoker,
            context,
            call_id="invalid-query-compile",
            tool_name="query.compile",
            input_data=QueryCompileInput(plan=invalid),
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(
            result.structured_error.code,
            ToolErrorCode.SQL_COMPILE_ERROR,
        )
        self.assertIn("governed query program", result.structured_error.message)


if __name__ == "__main__":
    unittest.main()
