"""初始化 Docker 部署所需状态，然后启动后端进程。"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

import asyncpg

from api.auth import hash_password
from api.auth_store import PostgresAuthStore


def database_url_from_environment(environment: Mapping[str, str]) -> str:
    """读取显式连接地址，或从 Compose 注入的独立字段安全构造地址。"""

    explicit = (
        environment.get("AUTH_DATABASE_URL", "").strip()
        or environment.get("DATABASE_URL", "").strip()
    )
    if explicit:
        return explicit

    values = {
        "host": environment.get("AUTH_DATABASE_HOST", "").strip(),
        "port": environment.get("AUTH_DATABASE_PORT", "5432").strip(),
        "database": environment.get("AUTH_DATABASE_NAME", "").strip(),
        "user": environment.get("AUTH_DATABASE_USER", "").strip(),
        "password": environment.get("AUTH_DATABASE_PASSWORD", ""),
    }
    missing = tuple(
        name
        for name in ("host", "database", "user", "password")
        if not values[name]
    )
    if missing:
        raise RuntimeError(
            "Docker 鉴权数据库配置不完整，缺少：" + "、".join(missing)
        )
    try:
        port = int(values["port"])
    except ValueError as exc:
        raise RuntimeError("AUTH_DATABASE_PORT 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("AUTH_DATABASE_PORT 必须在 1 到 65535 之间")

    host = values["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return (
        "postgresql://"
        f"{quote(values['user'], safe='')}:{quote(values['password'], safe='')}"
        f"@{host}:{port}/{quote(values['database'], safe='')}"
    )


def bootstrap_identity(
    environment: Mapping[str, str],
) -> tuple[str, str, str, str, list[str]]:
    """校验并返回首次启动管理员信息。"""

    tenant_id = environment.get("BOOTSTRAP_ADMIN_TENANT_ID", "demo").strip()
    user_id = environment.get(
        "BOOTSTRAP_ADMIN_USER_ID", "docker-admin"
    ).strip()
    username = environment.get("BOOTSTRAP_ADMIN_USERNAME", "admin").strip()
    password = environment.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    roles = list(
        dict.fromkeys(
            role.strip()
            for role in environment.get(
                "BOOTSTRAP_ADMIN_ROLES", "admin,user"
            ).split(",")
            if role.strip()
        )
    )
    missing = tuple(
        name
        for name, value in (
            ("BOOTSTRAP_ADMIN_TENANT_ID", tenant_id),
            ("BOOTSTRAP_ADMIN_USER_ID", user_id),
            ("BOOTSTRAP_ADMIN_USERNAME", username),
            ("BOOTSTRAP_ADMIN_PASSWORD", password),
            ("BOOTSTRAP_ADMIN_ROLES", roles),
        )
        if not value
    )
    if missing:
        raise RuntimeError("管理员初始化配置不完整，缺少：" + "、".join(missing))
    return tenant_id, user_id, username, password, roles


async def connect_with_retry(
    dsn: str,
    *,
    attempts: int = 30,
    interval_seconds: float = 2.0,
) -> asyncpg.Connection:
    """等待 Compose 中的 PostgreSQL 真正接受连接。"""

    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.connect(dsn, ssl=False)
        except (OSError, asyncpg.PostgresError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            if attempt == 1 or attempt % 5 == 0:
                print(
                    f"鉴权数据库尚未就绪，正在重试（{attempt}/{attempts}）……",
                    flush=True,
                )
            await asyncio.sleep(interval_seconds)
    raise RuntimeError("在限定时间内无法连接鉴权数据库") from last_error


async def initialize_database(
    dsn: str,
    environment: Mapping[str, str],
    *,
    schema_path: Path = Path("/app/db/auth.sql"),
) -> None:
    """幂等初始化鉴权表，并仅在账号不存在时创建管理员。"""

    connection = await connect_with_retry(dsn)
    try:
        schema = schema_path.read_text(encoding="utf-8")
        async with connection.transaction():
            await connection.execute(schema)
    finally:
        await connection.close()
    print("鉴权数据库结构已就绪。", flush=True)

    tenant_id, user_id, username, password, roles = bootstrap_identity(
        environment
    )
    store = PostgresAuthStore(dsn)
    by_username = await store.find_user_by_login(tenant_id, username)
    by_user_id = await store.get_user(tenant_id, user_id)
    if by_username is None and by_user_id is None:
        await store.upsert_user(
            tenant_id=tenant_id,
            user_id=user_id,
            username=username,
            password_hash=hash_password(password),
            roles=roles,
        )
        print(
            f"已创建初始管理员：租户 {tenant_id}，用户名 {username}。",
            flush=True,
        )
        return
    if (
        by_username is not None
        and by_user_id is not None
        and by_username["user_id"] == user_id
        and by_user_id["username"] == username
    ):
        print("初始管理员已存在，未覆盖其密码和角色。", flush=True)
        return
    raise RuntimeError(
        "初始管理员的用户名或用户编号与现有账号冲突，请修改 Docker 配置"
    )


def main(arguments: Sequence[str] | None = None) -> None:
    command = tuple(sys.argv[1:] if arguments is None else arguments)
    if not command:
        raise RuntimeError("容器启动命令不能为空")

    dsn = database_url_from_environment(os.environ)
    os.environ["AUTH_DATABASE_URL"] = dsn
    asyncio.run(initialize_database(dsn, os.environ))
    os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
