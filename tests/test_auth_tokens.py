from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
from fastapi import HTTPException

from api.auth import (
    AuthPrincipal,
    AuthSettings,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    load_auth_settings,
    verify_password,
)


class AuthTokenTests(unittest.TestCase):
    def setUp(self):
        self.settings = AuthSettings(secret_key="test-secret-that-is-at-least-32-bytes")
        self.principal = AuthPrincipal(
            tenant_id="tenant-1",
            user_id="user-1",
            username="alice",
            roles=["admin", "analyst"],
            token_version=3,
            token_id="access-token-id",
        )

    def make_access_token(self, **claim_overrides):
        now = datetime.now(UTC)
        payload = {
            "typ": "access",
            "sub": self.principal.user_id,
            "tenant_id": self.principal.tenant_id,
            "username": self.principal.username,
            "roles": self.principal.roles,
            "ver": self.principal.token_version,
            "iss": self.settings.issuer,
            "aud": self.settings.audience,
            "iat": now,
            "exp": now + timedelta(minutes=30),
            "jti": self.principal.token_id,
        }
        for key, value in claim_overrides.items():
            if value is None:
                payload.pop(key)
            else:
                payload[key] = value
        return jwt.encode(
            payload,
            self.settings.secret_key,
            algorithm=self.settings.algorithm,
        )

    def assert_rejected_as_unauthorized(self, token, expected_type="access"):
        with self.assertRaises(HTTPException) as caught:
            decode_token(token, expected_type, self.settings)

        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(caught.exception.headers, {"WWW-Authenticate": "Bearer"})

    def test_password_hash_verifies_correct_password_and_rejects_wrong(self):
        password_hash = hash_password("correct horse battery staple")

        self.assertTrue(verify_password("correct horse battery staple", password_hash))
        self.assertFalse(verify_password("wrong password", password_hash))

    def test_access_token_decodes_as_access(self):
        token = create_access_token(self.principal, self.settings)

        decoded = decode_token(token, "access", self.settings)

        self.assertEqual(decoded.tenant_id, "tenant-1")
        self.assertEqual(decoded.user_id, "user-1")
        self.assertEqual(decoded.username, "alice")
        self.assertEqual(decoded.roles, ["admin", "analyst"])
        self.assertEqual(decoded.token_version, 3)
        self.assertEqual(decoded.token_id, "access-token-id")

    def test_refresh_token_is_rejected_as_access(self):
        _, token, _ = create_refresh_token(self.principal, self.settings)

        self.assert_rejected_as_unauthorized(token)

    def test_expired_token_returns_401(self):
        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "typ": "access",
                "sub": self.principal.user_id,
                "tenant_id": self.principal.tenant_id,
                "username": self.principal.username,
                "roles": self.principal.roles,
                "ver": self.principal.token_version,
                "iss": self.settings.issuer,
                "aud": self.settings.audience,
                "iat": now - timedelta(hours=2),
                "exp": now - timedelta(hours=1),
                "jti": self.principal.token_id,
            },
            self.settings.secret_key,
            algorithm=self.settings.algorithm,
        )

        self.assert_rejected_as_unauthorized(token)

    def test_signed_token_missing_exp_is_rejected(self):
        token = self.make_access_token(exp=None)

        self.assert_rejected_as_unauthorized(token)

    def test_signed_token_missing_iat_is_rejected(self):
        token = self.make_access_token(iat=None)

        self.assert_rejected_as_unauthorized(token)

    def test_invalid_issuer_is_rejected(self):
        token = self.make_access_token(iss="other-issuer")

        self.assert_rejected_as_unauthorized(token)

    def test_invalid_audience_is_rejected(self):
        token = self.make_access_token(aud="other-audience")

        self.assert_rejected_as_unauthorized(token)

    def test_malformed_access_roles_as_dict_is_rejected(self):
        token = self.make_access_token(roles={"admin": True})

        self.assert_rejected_as_unauthorized(token)

    def test_malformed_required_claim_numeric_sub_is_rejected(self):
        token = self.make_access_token(sub=123)

        self.assert_rejected_as_unauthorized(token)

    def test_string_iat_is_rejected(self):
        token = self.make_access_token(iat="123")

        self.assert_rejected_as_unauthorized(token)

    def test_string_exp_is_rejected(self):
        now = datetime.now(UTC)
        token = self.make_access_token(iat=now, exp="9999999999")

        self.assert_rejected_as_unauthorized(token)

    def test_missing_secret_key_raises_runtime_error(self):
        with patch("api.auth.load_project_environment", return_value=False):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    load_auth_settings()


if __name__ == "__main__":
    unittest.main()
