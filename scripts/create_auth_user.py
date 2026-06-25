from __future__ import annotations

import argparse
import asyncio
from typing import Any

from api.auth import hash_password
from api.auth_store import create_auth_store


def build_user_payload(
    tenant_id: str,
    user_id: str,
    username: str,
    password: str,
    roles: list[str],
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id.strip(),
        "user_id": user_id.strip(),
        "username": username.strip(),
        "password_hash": hash_password(password),
        "roles": list(roles),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or update an auth user.")
    parser.add_argument("--tenant-id", default="demo")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--roles", nargs="+", default=["user"])
    parser.add_argument("--disabled", action="store_true")
    return parser.parse_args()


async def create_user(args: argparse.Namespace) -> dict[str, Any]:
    store = create_auth_store()
    payload = build_user_payload(
        tenant_id=args.tenant_id,
        user_id=args.user_id,
        username=args.username,
        password=args.password,
        roles=args.roles,
    )
    return await store.upsert_user(**payload, disabled=args.disabled)


def main() -> None:
    asyncio.run(create_user(parse_args()))


if __name__ == "__main__":
    main()
