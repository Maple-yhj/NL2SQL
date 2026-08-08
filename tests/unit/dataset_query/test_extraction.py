from __future__ import annotations

import inspect
from decimal import Decimal

from data_agent.dataset_query import (
    DatasetQueryCompiler,
    DatasetQueryExecutor,
    DatasetQueryPlan,
    answer_for_result,
    chart_for_result,
    tabular_rows,
)
from data_agent.tools.schemas import QueryRow, TabularResult


def test_extracted_services_are_owned_outside_the_api_layer() -> None:
    assert DatasetQueryPlan.__module__ == "data_agent.dataset_query.models"
    assert DatasetQueryCompiler.__module__ == "data_agent.dataset_query.compiler"
    assert DatasetQueryExecutor.__module__ == "data_agent.dataset_query.executor"
    for value in (DatasetQueryPlan, DatasetQueryCompiler, DatasetQueryExecutor):
        source = inspect.getsource(inspect.getmodule(value))
        assert "from api" not in source
        assert "import api" not in source


def test_result_rendering_is_pure_and_preserves_existing_contract() -> None:
    plan = DatasetQueryPlan(
        analysis_type="aggregate",
        aggregations=(
            {"ref": "dataset.Orders.total", "operation": "sum", "alias": "total"},
        ),
        group_by=("dataset.Orders.state",),
    )
    result = TabularResult(
        columns=("state", "total"),
        rows=(
            QueryRow(values=("RJ", Decimal("5"))),
            QueryRow(values=("SP", Decimal("30"))),
        ),
    )

    rows = tabular_rows(result)
    chart = chart_for_result(result, plan=plan, title="Revenue by state")
    answer = answer_for_result(result, chart=chart)

    assert [row.root for row in rows] == [
        {"state": "RJ", "total": "5"},
        {"state": "SP", "total": "30"},
    ]
    assert chart is not None
    assert chart.x_field == "state"
    assert "state=SP" in answer
