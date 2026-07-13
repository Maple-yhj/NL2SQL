"""Deterministic policy-decision identities shared by compile and execute."""

from __future__ import annotations

from .composition import ResolvedRuntimeBundle, stable_digest
from .models import PrincipalContext


def compute_policy_decision_id(
    bundle: ResolvedRuntimeBundle,
    principal: PrincipalContext,
    logical_plan_hash: str | None,
) -> str:
    """Hash only trusted authority and principal inputs, never caller claims."""

    return "policy_" + stable_digest(
        {
            "bundle": bundle.digest,
            "user": principal.user_id,
            "tenant": principal.tenant_id,
            "roles": tuple(sorted(set(principal.roles))),
            "logical_plan_hash": logical_plan_hash,
        }
    )
