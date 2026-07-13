from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from api.auth_store import create_auth_store
from scripts.create_auth_user import build_user_payload


OLIST_EVAL_ADMIN_TENANT_ID = "admin"
OLIST_EVAL_ADMIN_USER_ID = "olist-admin"
OLIST_EVAL_SELLER_TENANT_ID = "3442f8959a84dea7ee197c632cb2df15"
OLIST_EVAL_SELLER_USER_ID = f"olist-seller-{OLIST_EVAL_SELLER_TENANT_ID}"
DEFAULT_EVAL_USERNAME = "olist-eval"


def _resolve_credentials(
    *,
    username: str | None,
    password: str | None,
) -> tuple[str, str]:
    resolved_username = (username or os.getenv("EVAL_USERNAME") or DEFAULT_EVAL_USERNAME).strip()
    resolved_password = password or os.getenv("EVAL_PASSWORD")
    if not resolved_username:
        raise ValueError("OList eval username must not be blank")
    if not resolved_password:
        raise ValueError("OList eval password is required via --password or EVAL_PASSWORD")
    return resolved_username, resolved_password


def build_olist_eval_seller_payload(
    *,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    resolved_username, resolved_password = _resolve_credentials(
        username=username,
        password=password,
    )
    return build_user_payload(
        tenant_id=OLIST_EVAL_SELLER_TENANT_ID,
        user_id=OLIST_EVAL_SELLER_USER_ID,
        username=resolved_username,
        password=resolved_password,
        roles=["user"],
    )


def build_olist_eval_user_payloads(
    *,
    username: str | None = None,
    password: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_username, resolved_password = _resolve_credentials(
        username=username,
        password=password,
    )
    admin = build_user_payload(
        tenant_id=OLIST_EVAL_ADMIN_TENANT_ID,
        user_id=OLIST_EVAL_ADMIN_USER_ID,
        username=resolved_username,
        password=resolved_password,
        roles=["admin", "user"],
    )
    seller = build_olist_eval_seller_payload(
        username=resolved_username,
        password=resolved_password,
    )
    return admin, seller


async def seed_olist_eval_users(
    *,
    username: str | None = None,
    password: str | None = None,
    disabled: bool = False,
) -> tuple[dict[str, Any], ...]:
    store = create_auth_store()
    users = []
    for payload in build_olist_eval_user_payloads(
        username=username,
        password=password,
    ):
        users.append(await store.upsert_user(**payload, disabled=disabled))
    return tuple(users)


async def seed_olist_eval_seller_user(
    *,
    username: str | None = None,
    password: str | None = None,
    disabled: bool = False,
) -> dict[str, Any]:
    store = create_auth_store()
    payload = build_olist_eval_seller_payload(
        username=username,
        password=password,
    )
    return await store.upsert_user(**payload, disabled=disabled)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the OList seller eval auth user.")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--disabled", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        seed_olist_eval_users(
            username=args.username,
            password=args.password,
            disabled=args.disabled,
        )
    )


if __name__ == "__main__":
    main()
