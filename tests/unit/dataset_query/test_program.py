from __future__ import annotations

import pytest
from pydantic import ValidationError

from data_agent.dataset_query import DatasetQueryProgram


def _count_stage(stage_id: str = "orders") -> dict[str, object]:
    return {
        "kind": "query",
        "stage_id": stage_id,
        "input": {"kind": "dataset"},
        "projections": [
            {
                "alias": "row_count",
                "expression": {"kind": "aggregate", "operation": "count"},
            }
        ],
    }


def test_program_accepts_generic_multistage_aggregation() -> None:
    program = DatasetQueryProgram.model_validate(
        {
            "schema_version": 2,
            "stages": [
                {
                    "kind": "query",
                    "stage_id": "per_entity",
                    "input": {"kind": "dataset"},
                    "projections": [
                        {
                            "alias": "entity_id",
                            "expression": {"kind": "field", "ref": "domain.Event.entity_id"},
                        },
                        {
                            "alias": "event_count",
                            "expression": {
                                "kind": "aggregate",
                                "operation": "count",
                            },
                        },
                    ],
                    "group_by": [
                        {"kind": "field", "ref": "domain.Event.entity_id"}
                    ],
                },
                {
                    "kind": "query",
                    "stage_id": "summary",
                    "input": {"kind": "stage", "stage_id": "per_entity"},
                    "projections": [
                        {
                            "alias": "entities",
                            "expression": {"kind": "aggregate", "operation": "count"},
                        },
                        {
                            "alias": "repeat_entities",
                            "expression": {
                                "kind": "aggregate",
                                "operation": "count",
                                "filter": {
                                    "kind": "binary",
                                    "operation": "gte",
                                    "left": {
                                        "kind": "output",
                                        "stage_id": "per_entity",
                                        "name": "event_count",
                                    },
                                    "right": {"kind": "literal", "value": 2},
                                },
                            },
                        },
                    ],
                },
            ],
            "output_stage_id": "summary",
        }
    )

    assert program.output_stage_id == "summary"
    assert len(program.stages) == 2


def test_program_accepts_median_text_matching_and_calendar_month_offsets() -> None:
    program = DatasetQueryProgram.model_validate(
        {
            "stages": [
                {
                    "kind": "query",
                    "stage_id": "summary",
                    "input": {"kind": "dataset"},
                    "projections": [
                        {
                            "alias": "median_value",
                            "expression": {
                                "kind": "aggregate",
                                "operation": "median",
                                "operand": {"kind": "field", "ref": "domain.Event.value"},
                            },
                        },
                        {
                            "alias": "matched_rows",
                            "expression": {
                                "kind": "aggregate",
                                "operation": "count",
                                "filter": {
                                    "kind": "function",
                                    "operation": "contains_ci",
                                    "arguments": [
                                        {"kind": "field", "ref": "domain.Event.note"},
                                        {"kind": "literal", "value": "late"},
                                    ],
                                },
                            },
                        },
                    ],
                    "filters": [
                        {
                            "kind": "binary",
                            "operation": "gte",
                            "left": {
                                "kind": "function",
                                "operation": "date_diff_months",
                                "arguments": [
                                    {"kind": "field", "ref": "domain.Event.started_at"},
                                    {"kind": "field", "ref": "domain.Event.ended_at"},
                                ],
                            },
                            "right": {"kind": "literal", "value": 1},
                        }
                    ],
                }
            ],
            "output_stage_id": "summary",
        }
    )

    assert program.stages[0].projections[0].expression.operation == "median"


def test_program_rejects_forward_stage_references() -> None:
    with pytest.raises(ValidationError, match="unavailable prior stages"):
        DatasetQueryProgram.model_validate(
            {
                "stages": [
                    {
                        "kind": "query",
                        "stage_id": "summary",
                        "input": {"kind": "stage", "stage_id": "future"},
                        "projections": [
                            {
                                "alias": "row_count",
                                "expression": {
                                    "kind": "aggregate",
                                    "operation": "count",
                                },
                            }
                        ],
                    },
                    _count_stage("future"),
                ],
                "output_stage_id": "summary",
            }
        )


def test_non_ready_program_cannot_hide_executable_stages() -> None:
    with pytest.raises(ValidationError, match="cannot include executable stages"):
        DatasetQueryProgram.model_validate(
            {
                "status": "unsupported",
                "clarification_question": "A required semantic definition is unavailable.",
                "stages": [_count_stage()],
                "output_stage_id": "orders",
            }
        )


def test_union_requires_prior_unique_inputs() -> None:
    with pytest.raises(ValidationError, match="union inputs must be unique"):
        DatasetQueryProgram.model_validate(
            {
                "stages": [
                    _count_stage(),
                    {
                        "kind": "union_all",
                        "stage_id": "combined",
                        "input_stage_ids": ["orders", "orders"],
                    },
                ],
                "output_stage_id": "combined",
            }
        )
