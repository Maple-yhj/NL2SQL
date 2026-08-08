from __future__ import annotations

import json
import unittest
from pathlib import Path

from data_agent.runtime import AgentResponse


ROOT = Path(__file__).resolve().parents[1]


class FrontendAgentResponseSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = ROOT / "generated" / ".task9-agent-response-schema.json"
        self.fixture_output = ROOT / "generated" / ".task10-agent-response-fixture.json"
        if self.output.exists():
            self.output.unlink()
        if self.fixture_output.exists():
            self.fixture_output.unlink()

    def tearDown(self) -> None:
        if self.output.exists():
            self.output.unlink()
        if self.fixture_output.exists():
            self.fixture_output.unlink()

    def test_checked_in_schema_is_fresh_from_backend_openapi(self) -> None:
        from scripts.export_frontend_agent_response_schema import (
            build_frontend_agent_response_schema,
            export_frontend_agent_response_schema,
        )

        generated = export_frontend_agent_response_schema(self.output).read_bytes()
        checked_in = (
            ROOT
            / "frontend"
            / "src"
            / "generated"
            / "agent-response.schema.ts"
        ).read_bytes()

        self.assertEqual(generated, checked_in)
        source = generated.decode("utf-8")
        document = json.loads(source[source.index("{") : source.rindex("}") + 1])
        self.assertEqual(document["root"], "AgentResponse")
        checked_in_openapi = json.loads(
            (ROOT / "docs" / "apifox-openapi.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            document,
            build_frontend_agent_response_schema(checked_in_openapi),
        )
        self.assertEqual(
            set(document["schemas"]["AgentResponse"]["properties"]),
            set(AgentResponse.model_fields),
        )

    def test_frontend_fixture_is_an_exact_backend_serialization(self) -> None:
        from scripts.export_frontend_agent_response_schema import (
            export_frontend_agent_response_fixture,
        )

        generated = export_frontend_agent_response_fixture(
            self.fixture_output
        ).read_bytes()
        checked_in_path = (
            ROOT
            / "frontend"
            / "src"
            / "generated"
            / "agent-response.fixture.json"
        )
        self.assertEqual(generated, checked_in_path.read_bytes())
        fixture = json.loads(
            checked_in_path.read_text(encoding="utf-8")
        )

        response = AgentResponse.model_validate(fixture)

        self.assertEqual(
            fixture,
            response.model_dump(mode="json", by_alias=True),
        )

    def test_browser_validator_only_imports_the_packaged_generated_module(self) -> None:
        source = (
            ROOT / "frontend" / "src" / "agentResponseValidator.ts"
        ).read_text(encoding="utf-8")

        self.assertIn('./generated/agent-response.schema', source)
        self.assertNotIn("docs/", source)
        self.assertNotIn("apifox-openapi", source)


if __name__ == "__main__":
    unittest.main()
