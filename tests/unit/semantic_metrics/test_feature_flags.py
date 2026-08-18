from __future__ import annotations

import pytest

from data_agent.semantic_metrics import SemanticMetricFeatures


def test_rollout_flags_have_safe_defaults() -> None:
    features = SemanticMetricFeatures.from_environment({})

    assert features.domain_pack_discovery is True
    assert features.web_discovery is False
    assert features.provisional_overlays is False
    assert features.auto_publish_alias is False


def test_rollout_flags_parse_explicit_booleans_and_reject_typos() -> None:
    features = SemanticMetricFeatures.from_environment(
        {
            "SEMANTIC_METRICS_WEB_DISCOVERY": "yes",
            "SEMANTIC_METRICS_PROVISIONAL_OVERLAYS": "1",
        }
    )
    assert features.web_discovery is True
    assert features.provisional_overlays is True

    with pytest.raises(ValueError, match="must be a boolean"):
        SemanticMetricFeatures.from_environment(
            {"SEMANTIC_METRICS_WEB_DISCOVERY": "sometimes"}
        )
