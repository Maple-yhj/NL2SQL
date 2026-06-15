import unittest
from typing import get_type_hints

from graph.state import GraphState, InputState, OutputState


class GraphStateTests(unittest.TestCase):
    def test_input_state_has_only_public_request_fields(self):
        self.assertEqual(
            InputState.__required_keys__,
            frozenset({"question", "tenant_id", "execute"}),
        )

    def test_graph_state_contains_security_and_retry_channels(self):
        hints = get_type_hints(GraphState)
        for field in (
            "metrics_result",
            "schema_result",
            "table_names",
            "allowed_tables",
            "candidate_sql",
            "validated_sql",
            "validation_result",
            "validation_attempts",
            "retry_feedback",
            "execution_result",
            "trace",
        ):
            self.assertIn(field, hints)
        self.assertNotIn("llm", hints)
        self.assertNotIn("embeddings", hints)

    def test_output_state_is_stable_public_contract(self):
        self.assertEqual(
            OutputState.__required_keys__,
            frozenset(
                {
                    "ok",
                    "question",
                    "tenant_id",
                    "intent",
                    "sql",
                    "rows",
                    "answer",
                    "error",
                    "trace",
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
