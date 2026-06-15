import unittest

from graph.router import route_after_execute, route_after_schema, route_after_validate


class GraphRoutingTests(unittest.TestCase):
    def test_schema_requires_authorized_tables(self):
        self.assertEqual(route_after_schema({"allowed_tables": ["orders"]}), "generate_sql")
        self.assertEqual(route_after_schema({"allowed_tables": []}), "finalize")

    def test_validation_routes_retry_execute_or_finish(self):
        self.assertEqual(
            route_after_validate(
                {"validation_result": {"ok": False}, "validation_attempts": 1},
                max_attempts=2,
            ),
            "generate_sql",
        )
        self.assertEqual(
            route_after_validate(
                {"validation_result": {"ok": True}, "execute": True},
                max_attempts=2,
            ),
            "execute_sql",
        )
        self.assertEqual(
            route_after_validate(
                {"validation_result": {"ok": True}, "execute": False},
                max_attempts=2,
            ),
            "finalize",
        )

    def test_execution_failure_skips_explanation(self):
        self.assertEqual(route_after_execute({"execution_result": {"ok": False}}), "finalize")
        self.assertEqual(route_after_execute({"execution_result": {"ok": True}}), "explain")


if __name__ == "__main__":
    unittest.main()
