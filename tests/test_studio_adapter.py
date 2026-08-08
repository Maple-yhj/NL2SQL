from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StudioRuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_exports_the_native_compiled_agent_graph(self):
        from data_agent.adapters.studio import build_studio_graph, development_input

        studio_graph = build_studio_graph()
        document = studio_graph.get_graph().to_json()
        nodes = {item["id"] for item in document["nodes"]}
        self.assertIn("plan_or_replan", nodes)
        self.assertIn("guard_decision", nodes)
        self.assertIn("execute_tool", nodes)
        self.assertIn("evaluate_progress", nodes)
        self.assertIn("persist_turn", nodes)
        self.assertNotIn("runtime", nodes)

        self.assertIsNone(studio_graph.get_context_jsonschema())
        output = await studio_graph.ainvoke(development_input())
        self.assertEqual(output["status"].value, "completed")
        self.assertTrue(output["final_response"].ok)
        self.assertEqual(output["authority"].source_id, "studio-source")
        self.assertIn("offline plan mode", output["final_response"].answer)

    def test_studio_config_points_only_to_native_agent_graph(self):
        config = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8"))
        graph_targets = tuple(config["graphs"].values())

        self.assertEqual(
            graph_targets,
            ("./src/data_agent/adapters/studio.py:graph",),
        )
        serialized = json.dumps(config)
        for forbidden in ("graph/pipeline", "graph.pipeline", "execution"):
            self.assertNotIn(forbidden, serialized)

    def test_source_is_inert_and_does_not_build_runtime_or_datasource(self):
        source = (ROOT / "src" / "data_agent" / "adapters" / "studio.py").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "data_agent.execution",
            "graph.pipeline",
            "run_nl2sql",
            "graph.tools",
            "graph.memory_store",
            "build_upload_runtime",
            "DataSourceService",
            "runtime.run",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
