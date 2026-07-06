from __future__ import annotations

import argparse
import asyncio
from typing import Any

from api.auth_store import create_auth_store
from scripts.create_auth_user import build_user_payload


OLIST_EVAL_SELLER_TENANT_ID = "3442f8959a84dea7ee197c632cb2df15"
OLIST_EVAL_SELLER_USER_ID = f"olist-seller-{OLIST_EVAL_SELLER_TENANT_ID}"
OLIST_EVAL_USERNAME = "yehj"
OLIST_EVAL_PASSWORD = "0708"


def build_olist_eval_seller_payload() -> dict[str, Any]:
    return build_user_payload(
        tenant_id=OLIST_EVAL_SELLER_TENANT_ID,
        user_id=OLIST_EVAL_SELLER_USER_ID,
        username=OLIST_EVAL_USERNAME,
        password=OLIST_EVAL_PASSWORD,
        roles=["user"],
    )


async def seed_olist_eval_seller_user(*, disabled: bool = False) -> dict[str, Any]:
    store = create_auth_store()
    return await store.upsert_user(**build_olist_eval_seller_payload(), disabled=disabled)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the OList seller eval auth user.")
    parser.add_argument("--disabled", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(seed_olist_eval_seller_user(disabled=args.disabled))


if __name__ == "__main__":
    main()
