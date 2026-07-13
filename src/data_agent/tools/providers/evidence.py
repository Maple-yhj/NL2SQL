"""Process-local attestations for rows emitted by the execute provider."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from ..schemas import QueryRow
from .contracts import QueryData


class EvidenceSigner:
    """Prevent caller-constructed or mutated rows from becoming verified evidence."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or secrets.token_bytes(32)
        if len(self._key) < 32:
            raise ValueError("evidence signing key must be at least 256 bits")

    def sign(
        self,
        *,
        logical_plan_hash: str,
        query_hash: str,
        policy_decision_id: str,
        columns: tuple[str, ...],
        rows: tuple[QueryRow, ...],
    ) -> str:
        payload = {
            "logical_plan_hash": logical_plan_hash,
            "query_hash": query_hash,
            "policy_decision_id": policy_decision_id,
            "columns": columns,
            "rows": [row.model_dump(mode="json") for row in rows],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "evidence_" + hmac.new(
            self._key,
            encoded,
            hashlib.sha256,
        ).hexdigest()

    def verify(self, data: QueryData) -> bool:
        expected = self.sign(
            logical_plan_hash=data.logical_plan_hash,
            query_hash=data.query_hash,
            policy_decision_id=data.policy_decision_id,
            columns=data.columns,
            rows=data.rows,
        )
        return hmac.compare_digest(expected, data.verification_token)
