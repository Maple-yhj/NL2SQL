"""Fail-closed content policy for memory and persisted message summaries."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel


MAX_SAFE_TEXT_CHARACTERS = 4096
MAX_SAFE_COLLECTION_ITEMS = 100
MAX_POLICY_DEPTH = 12


class MemoryPolicyError(ValueError):
    """Raised when content is unsafe to persist."""


_FORBIDDEN_KEYS = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "credential",
    "credentials",
    "dsn",
    "database_url",
    "connection",
    "connection_string",
    "sql_params",
    "query_params",
    "raw_params",
    "parameters",
    "rows",
    "results",
    "full_results",
    "query_result",
    "domain_pack",
    "enterprise_binding",
    "deployment_profile",
    "physical_config",
    "policy",
    "policies",
    "allowed_relations",
    "permissions",
    "prompt",
    "raw_trace",
    "email",
    "phone",
    "address",
    "cpf",
    "ssn",
    "credit_card",
}
_CONNECTION_PATTERN = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|neo4j|"
    r"amqp|jdbc:[a-z0-9]+)://"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+"
)


def validate_candidate_content(value: Any) -> None:
    """Recursively reject dangerous payloads before they reach a provider."""

    _validate(value, path="$", depth=0)


def _validate(value: Any, *, path: str, depth: int) -> None:
    if depth > MAX_POLICY_DEPTH:
        raise MemoryPolicyError("content exceeds the maximum nesting depth")
    if isinstance(value, BaseModel):
        _validate(value.model_dump(mode="json"), path=path, depth=depth + 1)
        return
    if isinstance(value, Enum):
        _validate(value.value, path=path, depth=depth + 1)
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value) > MAX_SAFE_TEXT_CHARACTERS:
            raise MemoryPolicyError(f"text at {path} exceeds the safe size limit")
        if _CONNECTION_PATTERN.search(value) or _SECRET_ASSIGNMENT_PATTERN.search(value):
            raise MemoryPolicyError(f"secret or connection material is forbidden at {path}")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_SAFE_COLLECTION_ITEMS:
            raise MemoryPolicyError(f"mapping at {path} exceeds the safe item limit")
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_")
            if key in _FORBIDDEN_KEYS or any(
                key.endswith(f"_{suffix}")
                for suffix in ("password", "secret", "credential", "dsn")
            ):
                raise MemoryPolicyError(f"forbidden content field at {path}.{key}")
            _validate(item, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if len(value) > MAX_SAFE_COLLECTION_ITEMS:
            raise MemoryPolicyError(f"sequence at {path} exceeds the safe item limit")
        for index, item in enumerate(value):
            _validate(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    raise MemoryPolicyError(f"unsupported persisted content type at {path}")


__all__ = [
    "MAX_POLICY_DEPTH",
    "MAX_SAFE_COLLECTION_ITEMS",
    "MAX_SAFE_TEXT_CHARACTERS",
    "MemoryPolicyError",
    "validate_candidate_content",
]
