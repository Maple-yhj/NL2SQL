from __future__ import annotations

import unittest

from data_agent.analysis_agent.routing import route_after_evaluation, route_after_guard


class AgentRoutingTests(unittest.TestCase):
    def test_routes_accept_only_closed_server_destinations(self) -> None:
        self.assertEqual(route_after_guard({"next_route": "execute_tool"}), "execute_tool")
        self.assertEqual(
            route_after_evaluation({"next_route": "plan_or_replan"}),
            "plan_or_replan",
        )
        for state in (
            {"next_route": "model_supplied_node"},
            {"next_route": "plan_or_replan"},
            {},
        ):
            with self.assertRaises(ValueError):
                route_after_guard(state)


if __name__ == "__main__":
    unittest.main()
