import asyncio
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from api.auth import hash_password, verify_password
from api.auth_store import InMemoryAuthStore
from api.schemas import (
    AccessTokenResponse,
    AuthUserResponse,
    ConversationCreateRequest,
    ConversationMessageRequest,
    ConversationUpdateRequest,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    Nl2SqlRequest,
    RefreshRequest,
    TokenResponse,
)


class ApiAuthRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryAuthStore()
        asyncio.run(self.store.upsert_user(
            tenant_id="demo",
            user_id="user-1",
            username="alice",
            password_hash=hash_password("secret"),
            roles=["user", "admin"],
        ))
        asyncio.run(self.store.upsert_user(
            tenant_id="demo",
            user_id="user-2",
            username="disabled",
            password_hash=hash_password("secret"),
            roles=["user"],
            disabled=True,
        ))

        self.env_patch = mock.patch.dict(
            "os.environ",
            {"JWT_SECRET_KEY": "test-secret-key-with-at-least-32-bytes"},
        )
        self.store_patch = mock.patch(
            "api.routes.create_auth_store",
            return_value=self.store,
            create=True,
        )
        self.env_patch.start()
        self.store_patch.start()
        self.client = TestClient(create_app())

    def tearDown(self):
        self.store_patch.stop()
        self.env_patch.stop()

    def _login(self, username: str = "alice", password: str = "secret"):
        return self.client.post(
            "/api/auth/login",
            json={"tenant_id": "demo", "username": username, "password": password},
        )

    def test_login_returns_token_pair(self):
        response = self._login()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["access_token"])
        self.assertTrue(body["refresh_token"])
        self.assertEqual(body["token_type"], "bearer")
        self.assertEqual(body["expires_in"], 1800)
        self.assertEqual(
            body["user"],
            {
                "tenant_id": "demo",
                "user_id": "user-1",
                "username": "alice",
                "roles": ["user", "admin"],
            },
        )

    def test_login_rejects_wrong_password(self):
        response = self._login(password="wrong")

        self.assertEqual(response.status_code, 401)

    def test_login_rejects_disabled_user(self):
        response = self._login(username="disabled")

        self.assertEqual(response.status_code, 401)

    def test_refresh_rotates_token_and_old_refresh_token_cannot_be_reused(self):
        login = self._login()
        old_refresh_token = login.json()["refresh_token"]

        refresh = self.client.post(
            "/api/auth/refresh",
            json={"refresh_token": old_refresh_token},
        )

        self.assertEqual(refresh.status_code, 200)
        body = refresh.json()
        self.assertTrue(body["access_token"])
        self.assertTrue(body["refresh_token"])
        self.assertNotEqual(body["refresh_token"], old_refresh_token)

        reused = self.client.post(
            "/api/auth/refresh",
            json={"refresh_token": old_refresh_token},
        )
        self.assertEqual(reused.status_code, 401)

    def test_refresh_rejects_inactive_or_invalid_refresh_token(self):
        response = self.client.post(
            "/api/auth/refresh",
            json={"refresh_token": "not-a-refresh-token"},
        )

        self.assertEqual(response.status_code, 401)

    def test_me_returns_current_user(self):
        login = self._login()
        access_token = login.json()["access_token"]

        response = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "tenant_id": "demo",
                "user_id": "user-1",
                "username": "alice",
                "roles": ["user", "admin"],
            },
        )

    def test_me_rejects_missing_token(self):
        response = self.client.get("/api/auth/me")

        self.assertEqual(response.status_code, 401)

    def test_logout_revokes_supplied_refresh_token(self):
        login = self._login()
        refresh_token = login.json()["refresh_token"]

        logout = self.client.post(
            "/api/auth/logout",
            json={"refresh_token": refresh_token},
        )

        self.assertEqual(logout.status_code, 200)
        self.assertEqual(logout.json(), {"ok": True})

        refresh = self.client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        self.assertEqual(refresh.status_code, 401)

    def test_logout_rejects_invalid_refresh_token(self):
        response = self.client.post(
            "/api/auth/logout",
            json={"refresh_token": "not-a-token"},
        )

        self.assertEqual(response.status_code, 401)

    def test_logout_with_omitted_refresh_token_field_returns_ok_true(self):
        response = self.client.post("/api/auth/logout", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})


class ApiAuthSchemaTests(unittest.TestCase):
    def test_login_request_strips_text(self):
        request = LoginRequest(
            tenant_id=" demo ",
            username=" alice ",
            password=" secret ",
        )

        self.assertEqual(request.tenant_id, "demo")
        self.assertEqual(request.username, "alice")
        self.assertEqual(request.password, "secret")

    def test_login_request_rejects_blank_password(self):
        with self.assertRaises(ValidationError):
            LoginRequest(username="alice", password="   ")

    def test_login_request_rejects_blank_username(self):
        with self.assertRaises(ValidationError):
            LoginRequest(username="   ", password="secret")

    def test_refresh_request_rejects_blank_token(self):
        with self.assertRaises(ValidationError):
            RefreshRequest(refresh_token="   ")

    def test_logout_request_strips_provided_refresh_token(self):
        request = LogoutRequest(refresh_token=" refresh-token ")

        self.assertEqual(request.refresh_token, "refresh-token")

    def test_logout_request_defaults_refresh_token_to_empty_string(self):
        request = LogoutRequest()

        self.assertEqual(request.refresh_token, "")

    def test_auth_user_response_accepts_user_fields(self):
        response = AuthUserResponse(
            tenant_id="demo",
            user_id="user-1",
            username="alice",
            roles=["user", "admin"],
        )

        self.assertEqual(response.tenant_id, "demo")
        self.assertEqual(response.user_id, "user-1")
        self.assertEqual(response.username, "alice")
        self.assertEqual(response.roles, ["user", "admin"])

    def test_token_response_nests_user_and_defaults_token_type(self):
        user = AuthUserResponse(
            tenant_id="demo",
            user_id="user-1",
            username="alice",
            roles=["user"],
        )
        response = TokenResponse(
            access_token="access",
            refresh_token="refresh",
            expires_in=1800,
            user=user,
        )

        self.assertEqual(response.token_type, "bearer")
        self.assertEqual(response.user, user)

    def test_access_token_response_defaults_token_type(self):
        response = AccessTokenResponse(access_token="access", expires_in=1800)

        self.assertEqual(response.token_type, "bearer")

    def test_logout_response_defaults_ok_to_true(self):
        response = LogoutResponse()

        self.assertTrue(response.ok)

    def test_nl2sql_request_omitted_tenant_is_none_and_strips_question(self):
        request = Nl2SqlRequest(question=" show gmv ")

        self.assertIsNone(request.tenant_id)
        self.assertEqual(request.question, "show gmv")

    def test_nl2sql_request_strips_question_and_tenant(self):
        request = Nl2SqlRequest(question=" show gmv ", tenant_id=" demo ")

        self.assertEqual(request.question, "show gmv")
        self.assertEqual(request.tenant_id, "demo")

    def test_nl2sql_request_rejects_blank_question(self):
        with self.assertRaises(ValidationError):
            Nl2SqlRequest(question="   ")

    def test_nl2sql_request_rejects_blank_tenant_when_provided(self):
        with self.assertRaises(ValidationError):
            Nl2SqlRequest(question="show gmv", tenant_id="   ")

    def test_conversation_create_allows_omitted_identity_and_strips_title(self):
        request = ConversationCreateRequest(title=" Demo ")

        self.assertIsNone(request.tenant_id)
        self.assertIsNone(request.user_id)
        self.assertEqual(request.title, "Demo")

    def test_conversation_create_strips_identity_and_title(self):
        request = ConversationCreateRequest(
            tenant_id=" demo ",
            user_id=" user-1 ",
            title=" Demo ",
        )

        self.assertEqual(request.tenant_id, "demo")
        self.assertEqual(request.user_id, "user-1")
        self.assertEqual(request.title, "Demo")

    def test_conversation_create_rejects_blank_tenant_when_provided(self):
        with self.assertRaises(ValidationError):
            ConversationCreateRequest(tenant_id="   ", user_id="user-1")

    def test_conversation_create_rejects_blank_user_when_provided(self):
        with self.assertRaises(ValidationError):
            ConversationCreateRequest(tenant_id="demo", user_id="   ")

    def test_conversation_update_allows_omitted_identity_as_none(self):
        request = ConversationUpdateRequest(title="Demo")

        self.assertIsNone(request.tenant_id)
        self.assertIsNone(request.user_id)

    def test_conversation_update_strips_provided_identity(self):
        request = ConversationUpdateRequest(
            tenant_id=" demo ",
            user_id=" user-1 ",
        )

        self.assertEqual(request.tenant_id, "demo")
        self.assertEqual(request.user_id, "user-1")

    def test_conversation_update_rejects_blank_tenant_when_provided(self):
        with self.assertRaises(ValidationError):
            ConversationUpdateRequest(tenant_id="   ", user_id="user-1")

    def test_conversation_update_rejects_blank_user_when_provided(self):
        with self.assertRaises(ValidationError):
            ConversationUpdateRequest(tenant_id="demo", user_id="   ")

    def test_conversation_message_allows_omitted_identity_and_strips_question(self):
        request = ConversationMessageRequest(question=" show gmv ")

        self.assertIsNone(request.tenant_id)
        self.assertIsNone(request.user_id)
        self.assertEqual(request.question, "show gmv")

    def test_conversation_message_strips_question_and_identity(self):
        request = ConversationMessageRequest(
            question=" show gmv ",
            tenant_id=" demo ",
            user_id=" user-1 ",
        )

        self.assertEqual(request.question, "show gmv")
        self.assertEqual(request.tenant_id, "demo")
        self.assertEqual(request.user_id, "user-1")

    def test_conversation_message_rejects_blank_question(self):
        with self.assertRaises(ValidationError):
            ConversationMessageRequest(question="   ")

    def test_conversation_message_rejects_blank_tenant_when_provided(self):
        with self.assertRaises(ValidationError):
            ConversationMessageRequest(
                question="show gmv",
                tenant_id="   ",
                user_id="user-1",
            )

    def test_conversation_message_rejects_blank_user_when_provided(self):
        with self.assertRaises(ValidationError):
            ConversationMessageRequest(question="show gmv", user_id="   ")


class CreateAuthUserScriptTests(unittest.TestCase):
    def test_build_user_payload_hashes_password_and_strips_identity(self):
        from scripts.create_auth_user import build_user_payload

        payload = build_user_payload(
            tenant_id=" demo ",
            user_id=" user-1 ",
            username=" alice ",
            password="secret",
            roles=["user", "admin"],
        )

        self.assertEqual(payload["tenant_id"], "demo")
        self.assertEqual(payload["user_id"], "user-1")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["roles"], ["user", "admin"])
        self.assertNotEqual(payload["password_hash"], "secret")
        self.assertTrue(verify_password("secret", payload["password_hash"]))


if __name__ == "__main__":
    unittest.main()
