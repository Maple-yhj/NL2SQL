from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "docker" / "entrypoint.py"
SPEC = importlib.util.spec_from_file_location("docker_entrypoint", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
docker_entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(docker_entrypoint)


class DockerEntrypointConfigurationTests(unittest.TestCase):
    def test_explicit_database_url_takes_precedence(self) -> None:
        environment = {
            "AUTH_DATABASE_URL": "postgresql://explicit/database",
            "DATABASE_URL": "postgresql://fallback/database",
        }

        self.assertEqual(
            docker_entrypoint.database_url_from_environment(environment),
            "postgresql://explicit/database",
        )

    def test_database_components_are_url_encoded(self) -> None:
        environment = {
            "AUTH_DATABASE_HOST": "2001:db8::1",
            "AUTH_DATABASE_PORT": "5433",
            "AUTH_DATABASE_NAME": "data agent",
            "AUTH_DATABASE_USER": "agent@example.com",
            "AUTH_DATABASE_PASSWORD": "p@ss:/word",
        }

        self.assertEqual(
            docker_entrypoint.database_url_from_environment(environment),
            "postgresql://agent%40example.com:p%40ss%3A%2Fword"
            "@[2001:db8::1]:5433/data%20agent",
        )

    def test_incomplete_database_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "password"):
            docker_entrypoint.database_url_from_environment(
                {
                    "AUTH_DATABASE_HOST": "postgres",
                    "AUTH_DATABASE_NAME": "data_agent",
                    "AUTH_DATABASE_USER": "data_agent",
                }
            )

    def test_bootstrap_roles_are_trimmed_and_deduplicated(self) -> None:
        identity = docker_entrypoint.bootstrap_identity(
            {
                "BOOTSTRAP_ADMIN_TENANT_ID": " demo ",
                "BOOTSTRAP_ADMIN_USER_ID": " root ",
                "BOOTSTRAP_ADMIN_USERNAME": " admin ",
                "BOOTSTRAP_ADMIN_PASSWORD": "secret",
                "BOOTSTRAP_ADMIN_ROLES": "admin, user,admin",
            }
        )

        self.assertEqual(
            identity,
            ("demo", "root", "admin", "secret", ["admin", "user"]),
        )


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.close = AsyncMock()

    def transaction(self) -> _Transaction:
        return _Transaction()


class DockerEntrypointInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_creates_missing_admin(self) -> None:
        connection = _Connection()
        store = Mock()
        store.find_user_by_login = AsyncMock(return_value=None)
        store.get_user = AsyncMock(return_value=None)
        store.upsert_user = AsyncMock()
        environment = {
            "BOOTSTRAP_ADMIN_TENANT_ID": "demo",
            "BOOTSTRAP_ADMIN_USER_ID": "docker-admin",
            "BOOTSTRAP_ADMIN_USERNAME": "admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "secret",
            "BOOTSTRAP_ADMIN_ROLES": "admin,user",
        }

        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "auth.sql"
            schema_path.write_text("SELECT 1;", encoding="utf-8")
            with (
                patch.object(
                    docker_entrypoint,
                    "connect_with_retry",
                    AsyncMock(return_value=connection),
                ),
                patch.object(
                    docker_entrypoint,
                    "PostgresAuthStore",
                    return_value=store,
                ),
                patch.object(
                    docker_entrypoint,
                    "hash_password",
                    return_value="password-hash",
                ),
            ):
                await docker_entrypoint.initialize_database(
                    "postgresql://database",
                    environment,
                    schema_path=schema_path,
                )

        connection.execute.assert_awaited_once_with("SELECT 1;")
        connection.close.assert_awaited_once()
        store.upsert_user.assert_awaited_once_with(
            tenant_id="demo",
            user_id="docker-admin",
            username="admin",
            password_hash="password-hash",
            roles=["admin", "user"],
        )

    async def test_initialization_does_not_overwrite_existing_admin(self) -> None:
        connection = _Connection()
        existing = {"tenant_id": "demo", "user_id": "docker-admin", "username": "admin"}
        store = Mock()
        store.find_user_by_login = AsyncMock(return_value=existing)
        store.get_user = AsyncMock(return_value=existing)
        store.upsert_user = AsyncMock()
        environment = {
            "BOOTSTRAP_ADMIN_PASSWORD": "new-secret",
        }

        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "auth.sql"
            schema_path.write_text("SELECT 1;", encoding="utf-8")
            with (
                patch.object(
                    docker_entrypoint,
                    "connect_with_retry",
                    AsyncMock(return_value=connection),
                ),
                patch.object(
                    docker_entrypoint,
                    "PostgresAuthStore",
                    return_value=store,
                ),
            ):
                await docker_entrypoint.initialize_database(
                    "postgresql://database",
                    environment,
                    schema_path=schema_path,
                )

        store.upsert_user.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
