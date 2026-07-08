import unittest

from graph.tools.registry import ToolRegistry, ToolSpec, default_tool_registry


async def fake_handler(state, runtime, inputs):
    return {"ok": True}


class ToolRegistryTests(unittest.TestCase):
    def test_default_registry_exposes_dynamic_agent_tools(self):
        registry = default_tool_registry()

        for name in (
            "search_metrics",
            "search_schema",
            "generate_sql",
            "prepare_sql",
            "execute_sql",
            "explain_result",
            "explain_table_result",
        ):
            self.assertIn(name, registry.names())

        prepare_sql = registry.get("prepare_sql")
        self.assertEqual(prepare_sql.risk_level, "medium")
        self.assertIn("executable_sql", prepare_sql.output_keys)

    def test_registry_rejects_unknown_tools(self):
        registry = ToolRegistry()

        with self.assertRaisesRegex(KeyError, "Unknown tool"):
            registry.get("missing_tool")

    def test_registry_rejects_duplicate_tool_names(self):
        registry = ToolRegistry()
        spec = ToolSpec(
            name="demo",
            description="demo tool",
            aliases=("demo.alias",),
            input_keys=("question",),
            output_keys=("ok",),
            handler=fake_handler,
        )

        registry.register(spec)

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(spec)

    def test_registry_resolves_namespaced_aliases_to_canonical_tools(self):
        registry = default_tool_registry()

        aliases = {
            "metric.search": "search_metrics",
            "schema.search": "search_schema",
            "sql.generate": "generate_sql",
            "sql.prepare": "prepare_sql",
            "sql.validate": "validate_sql",
            "db.execute_readonly": "execute_sql",
            "answer.explain": "explain_result",
        }

        for alias, canonical in aliases.items():
            with self.subTest(alias=alias):
                self.assertIs(registry.get(alias), registry.get(canonical))
                self.assertEqual(registry.canonical_name(alias), canonical)

        self.assertNotIn("sql.prepare", registry.names())

    def test_registry_rejects_duplicate_aliases(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="first",
                description="first",
                aliases=("shared.alias",),
            )
        )

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(
                ToolSpec(
                    name="second",
                    description="second",
                    aliases=("shared.alias",),
                )
            )


if __name__ == "__main__":
    unittest.main()
