from __future__ import annotations

import json
import unittest
from pathlib import Path

from data_agent.runtime import AgentRequest, AgentResponse


ROOT = Path(__file__).resolve().parents[1]


class OpenApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = ROOT / "generated" / ".task9-openapi-test.json"
        if self.output.exists():
            self.output.unlink()

    def tearDown(self) -> None:
        if self.output.exists():
            self.output.unlink()

    def test_export_is_deterministic_strict_and_matches_runtime_contract(self):
        from scripts.export_apifox_openapi import export_openapi

        first = export_openapi(self.output).read_bytes()
        second = export_openapi(self.output).read_bytes()

        self.assertEqual(first, second)
        document = json.loads(first)
        self.assertEqual(document["info"]["title"], "Data Agent API")
        schemas = document["components"]["schemas"]
        request_schema = schemas["Nl2SqlRequest"]
        self.assertEqual(set(request_schema["properties"]), set(AgentRequest.model_fields))
        self.assertFalse(request_schema["additionalProperties"])
        for removed in (
            "tenant_id",
            "user_id",
            "agent_mode",
            "execute",
            "timeout_ms",
            "max_limit",
            "max_validation_attempts",
        ):
            self.assertNotIn(removed, request_schema["properties"])

        response_schema = schemas["AgentResponse"]
        self.assertEqual(set(response_schema["properties"]), set(AgentResponse.model_fields))
        self.assertFalse(response_schema["additionalProperties"])
        self.assertIn("version_pins", response_schema["properties"])
        self.assertEqual(
            document["paths"]["/api/conversations/{conversation_id}/messages"]["post"][
                "requestBody"
            ]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ConversationMessageRequest",
        )

    def test_checked_in_openapi_is_the_exact_export(self):
        from scripts.export_apifox_openapi import export_openapi

        generated = export_openapi(self.output).read_bytes()

        self.assertEqual(
            generated,
            (ROOT / "docs" / "apifox-openapi.json").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
