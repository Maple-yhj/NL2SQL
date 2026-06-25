import unittest
from unittest import mock

from fastapi.testclient import TestClient

from api.app import create_app
from api.auth import AuthPrincipal, AuthSettings, create_access_token


TEST_JWT_SECRET = "test-secret-key-with-at-least-32-bytes"


def auth_headers(
    tenant_id: str = "demo",
    user_id: str = "user-1",
    username: str = "analyst",
) -> dict[str, str]:
    token = create_access_token(
        AuthPrincipal(
            tenant_id=tenant_id,
            user_id=user_id,
            username=username,
            roles=["analyst"],
            token_version=1,
            token_id="test-token-id",
        ),
        AuthSettings(secret_key=TEST_JWT_SECRET),
    )
    return {"Authorization": f"Bearer {token}"}


def graph_output(**overrides):
    output = {
        "ok": True,
        "question": "show gmv",
        "tenant_id": "demo",
        "intent": {"metrics": ["gmv"]},
        "sql": "SELECT sum(amount) AS gmv FROM orders LIMIT 1000",
        "rows": [],
        "answer": "",
        "error": "",
        "trace": [{"node": "initialize", "ok": True, "message": "success"}],
    }
    output.update(overrides)
    return output


class ApiNl2SqlTests(unittest.TestCase):
    def test_nl2sql_calls_graph_pipeline_and_returns_output(self):
        client = TestClient(create_app())

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}), mock.patch(
            "api.routes.run_nl2sql",
            new=mock.AsyncMock(return_value=graph_output()),
        ) as run_nl2sql:
            response = client.post(
                "/api/nl2sql",
                headers=auth_headers(),
                json={
                    "question": " show gmv ",
                    "tenant_id": "demo",
                    "execute": False,
                    "timeout_ms": 5000,
                    "max_limit": 200,
                    "max_validation_attempts": 3,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sql"], "SELECT sum(amount) AS gmv FROM orders LIMIT 1000")
        run_nl2sql.assert_awaited_once_with(
            "show gmv",
            tenant_id="demo",
            execute=False,
            timeout_ms=5000,
            max_limit=200,
            max_validation_attempts=3,
        )

    def test_nl2sql_requires_token(self):
        client = TestClient(create_app())

        response = client.post("/api/nl2sql", json={"question": "show gmv"})

        self.assertEqual(response.status_code, 401)

    def test_nl2sql_rejects_mismatched_tenant_id(self):
        client = TestClient(create_app())

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}):
            response = client.post(
                "/api/nl2sql",
                headers=auth_headers(tenant_id="demo"),
                json={"question": "show gmv", "tenant_id": "other"},
            )

        self.assertEqual(response.status_code, 403)

    def test_nl2sql_uses_token_tenant_when_request_omits_tenant_id(self):
        client = TestClient(create_app())

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}), mock.patch(
            "api.routes.run_nl2sql",
            new=mock.AsyncMock(return_value=graph_output(tenant_id="token-tenant")),
        ) as run_nl2sql:
            response = client.post(
                "/api/nl2sql",
                headers=auth_headers(tenant_id="token-tenant"),
                json={"question": "show gmv"},
            )

        self.assertEqual(response.status_code, 200)
        run_nl2sql.assert_awaited_once_with(
            "show gmv",
            tenant_id="token-tenant",
            execute=False,
            timeout_ms=10000,
            max_limit=1000,
            max_validation_attempts=2,
        )

    def test_nl2sql_rejects_blank_question(self):
        client = TestClient(create_app())

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}):
            response = client.post(
                "/api/nl2sql",
                headers=auth_headers(),
                json={"question": "   "},
            )

        self.assertEqual(response.status_code, 422)

    def test_nl2sql_rejects_invalid_limit(self):
        client = TestClient(create_app())

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}):
            response = client.post(
                "/api/nl2sql",
                headers=auth_headers(),
                json={"question": "show gmv", "max_limit": 0},
            )

        self.assertEqual(response.status_code, 422)

    def test_nl2sql_converts_unhandled_exception_to_500(self):
        client = TestClient(create_app(), raise_server_exceptions=False)

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}), mock.patch(
            "api.routes.run_nl2sql",
            new=mock.AsyncMock(side_effect=RuntimeError("provider unavailable")),
        ):
            response = client.post(
                "/api/nl2sql",
                headers=auth_headers(),
                json={"question": "show gmv"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {
                "ok": False,
                "error": "Internal server error",
                "detail": "provider unavailable",
            },
        )


if __name__ == "__main__":
    unittest.main()
