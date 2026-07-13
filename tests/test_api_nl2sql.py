from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from api.app import create_app
from tests.test_api_runtime_contract import (
    TEST_JWT_SECRET,
    _RecordingComposition,
    _auth_headers,
)


class ApiNl2SqlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.composition = _RecordingComposition()
        self.env = mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET})
        self.env.start()
        app = create_app(
            runtime_factory=mock.AsyncMock(return_value=self.composition)
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.env.stop()

    def test_nl2sql_requires_token(self):
        response = self.client.post("/api/nl2sql", json={"question": "show gmv"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.composition.runtime.calls, [])

    def test_all_three_modes_are_forwarded_to_the_same_runtime(self):
        for mode in ("plan", "preview", "execute"):
            with self.subTest(mode=mode):
                response = self.client.post(
                    "/api/nl2sql",
                    headers=_auth_headers(),
                    json={"question": "show gmv", "mode": mode},
                )
                self.assertEqual(response.status_code, 200)

        self.assertEqual(
            [request.mode.value for request, _ in self.composition.runtime.calls],
            ["plan", "preview", "execute"],
        )

    def test_legacy_execution_and_budget_fields_are_explicitly_rejected(self):
        forbidden_values = {
            "agent_mode": "dynamic",
            "execute": True,
            "timeout_ms": 10_000,
            "max_limit": 1_000,
            "max_validation_attempts": 2,
            "memory_history_limit": 8,
            "include_tool_trace": True,
        }
        for field, value in forbidden_values.items():
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/nl2sql",
                    headers=_auth_headers(),
                    json={"question": "show gmv", field: value},
                )
                self.assertEqual(response.status_code, 422)

        self.assertEqual(self.composition.runtime.calls, [])

    def test_request_uses_token_principal_and_cannot_accept_identity_fields(self):
        for field in ("tenant_id", "user_id", "roles"):
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/nl2sql",
                    headers=_auth_headers(),
                    json={"question": "show gmv", field: "attacker"},
                )
                self.assertEqual(response.status_code, 422)

        self.assertEqual(self.composition.runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
