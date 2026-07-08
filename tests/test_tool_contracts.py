import unittest

from pydantic import ValidationError

from graph.tools.contracts import (
    ExecuteSqlInput,
    ExecuteSqlOutput,
    PrepareSqlInput,
    PrepareSqlOutput,
    ToolError,
    ToolResult,
    ToolSpec,
    ValidateSqlInput,
    ValidateSqlOutput,
)
from graph.tools.registry import default_tool_registry


class ToolContractTests(unittest.TestCase):
    def test_tool_result_accepts_success_and_recoverable_error_shapes(self):
        success = ToolResult(
            ok=True,
            data={"validated_sql": "SELECT 1"},
            summary="SQL validated",
        )

        failure = ToolResult(
            ok=False,
            error=ToolError(
                code="unknown_table_alias",
                message="Alias x is not defined.",
                recoverable=True,
                retry_hint="Use a declared table alias.",
            ),
        )

        self.assertIsNone(success.error)
        self.assertEqual(success.data["validated_sql"], "SELECT 1")
        self.assertEqual(failure.error.code, "unknown_table_alias")
        self.assertTrue(failure.error.recoverable)
        self.assertEqual(failure.error.retry_hint, "Use a declared table alias.")

    def test_failed_tool_result_requires_error(self):
        with self.assertRaises(ValidationError):
            ToolResult(ok=False)

    def test_tool_spec_validates_core_metadata(self):
        spec = ToolSpec(
            name="sql.prepare",
            description="Prepare SQL for safe execution.",
            input_schema=PrepareSqlInput,
            output_schema=PrepareSqlOutput,
            risk_level="medium",
            side_effects="none",
        )

        self.assertEqual(spec.name, "sql.prepare")
        self.assertEqual(spec.risk_level, "medium")
        self.assertEqual(spec.side_effects, "none")

        with self.assertRaises(ValidationError):
            ToolSpec(name="", description="blank")

        with self.assertRaises(ValidationError):
            ToolSpec(name="bad", description="bad", risk_level="dangerous")

        with self.assertRaises(ValidationError):
            ToolSpec(name="bad", description="bad", side_effects="mutate")

    def test_sql_tool_schema_fields_are_declared(self):
        registry = default_tool_registry()
        prepare = registry.get("prepare_sql")

        self.assertIs(prepare.input_schema, PrepareSqlInput)
        self.assertIs(prepare.output_schema, PrepareSqlOutput)
        self.assertEqual(prepare.risk_level, "medium")
        self.assertEqual(prepare.side_effects, "none")

        input_fields = set(prepare.input_schema.model_fields)
        output_fields = set(prepare.output_schema.model_fields)

        self.assertGreaterEqual(
            input_fields,
            {"sql", "tenant_id", "allowed_tables", "max_limit", "domain_constraints"},
        )
        self.assertGreaterEqual(output_fields, {"sql", "executable_sql", "violations"})

    def test_validate_and_execute_sql_schemas_are_declared(self):
        registry = default_tool_registry()

        validate = registry.get("validate_sql")
        self.assertIs(validate.input_schema, ValidateSqlInput)
        self.assertIs(validate.output_schema, ValidateSqlOutput)

        execute = registry.get("execute_sql")
        self.assertIs(execute.input_schema, ExecuteSqlInput)
        self.assertIs(execute.output_schema, ExecuteSqlOutput)
        self.assertEqual(execute.risk_level, "high")


if __name__ == "__main__":
    unittest.main()
