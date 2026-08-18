from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_agent.semantic_metrics import DomainPackRegistry


_CASES = json.loads(
    (
        Path(__file__).parents[2]
        / "fixtures"
        / "semantic_metric_cases.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_domain_metric_detection_eval(case: dict[str, object]) -> None:
    matches = DomainPackRegistry().detect_templates(
        str(case["question"]),
        domain_id=str(case["domain_id"]),
    )

    expected_ref = case["expected_metric_ref"]
    if expected_ref is None:
        assert matches == ()
        return
    assert matches[0][1].metric_ref == expected_ref
    assert matches[0][2] == case["expected_term"]
