import unittest
from typing import get_type_hints

from graph.state import GraphState, InputState, OutputState


class GraphStateTests(unittest.TestCase):
    def test_input_state_has_only_public_request_fields(self):
        self.assertEqual(
            InputState.__required_keys__,
            frozenset({"question", "tenant_id", "execute"}),
        )
        self.assertEqual(
            InputState.__optional_keys__,
            frozenset({"conversation_id", "user_id"}),
        )

    def test_graph_state_contains_security_and_retry_channels(self):
        hints = get_type_hints(GraphState)
        for field in (
            "plan",
            "execution_graph",
            "planned_intent",
            "plan_context",
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
            "conversation_id",
            "user_id",
            "conversation_history",
            "user_memories",
            "contextualized_question",
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
                    "contextualized_question",
                    "conversation_id",
                    "user_id",
                    "tenant_id",
                    "intent",
                    "sql",
                    "message_type",
                    "rows",
                    "answer",
                    "error",
                    "trace",
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
