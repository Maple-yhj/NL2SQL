from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.execution import (
    ExecutionAuthorityPin,
    ExecutionCheckpoint,
    ExecutionState,
    ExecutionStatus,
    ExecutionVersionPins,
    VersionPin,
)
from data_agent.memory.manager import MemoryApprovalError, MemoryConflictError
from data_agent.memory.models import (
    ApprovalContext,
    ApprovalDecision,
    ArtifactReference,
    Checkpoint,
    ConversationSummaryWrite,
    ConversationWriteBatch,
    EnterpriseMemoryContent,
    EnterpriseMemoryOwner,
    MemoryBudget,
    MemoryCandidate,
    MemoryQuery,
    MemoryScope,
    MessageRole,
    MessageWrite,
    SafeMessagePayload,
    SubjectScope,
    UserMemoryContent,
    UserMemoryOwner,
    WorkingMemoryContent,
    WorkingMemoryOwner,
)
from data_agent.memory.providers.postgres import PostgresMemoryManager
from data_agent.runtime.models import AgentMode


NOW = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
_MARKER = re.compile(r"/\*\s*(memory:[a-z_]+)\s*\*/")


def _marker(sql: str) -> str:
    match = _MARKER.search(sql)
    if match is None:
        raise AssertionError(f"SQL is missing a stable operation marker: {sql}")
    return match.group(1)


@dataclass(frozen=True)
class _SqlShape:
    kind: str
    table: str
    columns: frozenset[str]
    assignments: frozenset[str]
    placeholders: frozenset[int]
    returning: bool
    for_update: bool
    joins: tuple[str, ...]
    canonical_sql: str


@dataclass(frozen=True)
class _SqlRule:
    api: str
    kind: str
    table: str
    placeholders: frozenset[int]
    columns: frozenset[str] = frozenset()
    assignments: frozenset[str] = frozenset()
    required_predicates: tuple[str, ...] = ()
    returning: bool = False
    for_update: bool = False
    joins: tuple[str, ...] = ()


def _indices(last: int) -> frozenset[int]:
    return frozenset(range(1, last + 1))


def _parse_sql_shape(sql: str) -> _SqlShape:
    try:
        expression = sqlglot.parse_one(sql, read="postgres")
    except (ParseError, ValueError) as exc:
        raise AssertionError(f"SQL shape is not valid PostgreSQL: {exc}") from exc
    if expression is None:
        raise AssertionError("SQL shape is empty")
    normalized = " ".join(expression.sql(dialect="postgres").lower().split())
    normalized = re.sub(r"/\*.*?\*/", "", normalized).strip()
    canonical = re.sub(r"\b[a-z_][a-z0-9_]*\.", "", normalized)
    kind = str(expression.key).upper()
    target_patterns = {
        "INSERT": r"\binsert\s+into\s+([a-z_][a-z0-9_]*)",
        "UPDATE": r"\bupdate\s+([a-z_][a-z0-9_]*)\s+set\b",
        "DELETE": r"\bdelete\s+from\s+([a-z_][a-z0-9_]*)",
        "SELECT": r"\bfrom\s+([a-z_][a-z0-9_]*)",
    }
    pattern = target_patterns.get(kind)
    match = re.search(pattern, canonical) if pattern is not None else None
    if match is None:
        raise AssertionError(f"SQL shape has no supported {kind or 'statement'} target")
    table = match.group(1)
    columns: frozenset[str] = frozenset()
    if kind == "INSERT":
        column_match = re.search(
            rf"\binsert\s+into\s+{re.escape(table)}\s*\((.*?)\)\s*values\b",
            canonical,
        )
        if column_match is None:
            raise AssertionError("SQL shape INSERT must declare its target columns")
        columns = frozenset(
            value.strip() for value in column_match.group(1).split(",")
        )
    assignments: frozenset[str] = frozenset()
    if kind == "UPDATE":
        assignment_match = re.search(
            rf"\bupdate\s+{re.escape(table)}\s+set\s+(.*?)\s+where\b",
            canonical,
        )
        if assignment_match is None:
            raise AssertionError("SQL shape UPDATE must have a bounded WHERE clause")
        assignments = frozenset(
            item.split("=", 1)[0].strip()
            for item in assignment_match.group(1).split(",")
        )
    return _SqlShape(
        kind=kind,
        table=table,
        columns=columns,
        assignments=assignments,
        placeholders=frozenset(
            int(value) for value in re.findall(r"\$(\d+)", canonical)
        ),
        returning=bool(re.search(r"\breturning\b", canonical)),
        for_update=" for update" in canonical,
        joins=tuple(re.findall(r"\bjoin\s+([a-z_][a-z0-9_]*)", canonical)),
        canonical_sql=canonical,
    )


_SQL_RULES: dict[str, _SqlRule] = {
    "memory:create_conversation": _SqlRule(
        "fetchrow", "INSERT", "data_agent_conversations", _indices(8),
        frozenset({"tenant_id", "user_id", "domain_id", "conversation_id", "owner_key", "title", "summary", "status", "created_at", "updated_at"}),
        returning=True,
    ),
    "memory:get_conversation": _SqlRule(
        "fetchrow", "SELECT", "data_agent_conversations", _indices(5),
        required_predicates=("tenant_id = $1", "user_id = $2", "domain_id = $3", "conversation_id = $4", "owner_key = $5"),
    ),
    "memory:lock_conversation": _SqlRule(
        "fetchrow", "SELECT", "data_agent_conversations", _indices(5),
        required_predicates=("tenant_id = $1", "user_id = $2", "domain_id = $3", "conversation_id = $4", "owner_key = $5"),
        for_update=True,
    ),
    "memory:insert_proposal": _SqlRule(
        "fetchrow", "INSERT", "data_agent_memory_proposals", _indices(13),
        frozenset({"proposal_id", "candidate_digest", "owner_key", "tenant_id", "scope", "user_id", "domain_id", "conversation_id", "run_id", "candidate_json", "deduplication_key", "status", "proposed_at", "updated_at"}),
        required_predicates=("owner_key = $3", "tenant_id = $4", "scope = $5", "deduplication_key = $11"),
        returning=True,
    ),
    "memory:lock_proposal": _SqlRule(
        "fetchrow", "SELECT", "data_agent_memory_proposals", _indices(2),
        required_predicates=("proposal_id = $1", "tenant_id = $2"),
        for_update=True,
    ),
    "memory:insert_record": _SqlRule(
        "fetchrow", "INSERT", "data_agent_memory_records", _indices(22),
        frozenset({"memory_id", "proposal_id", "owner_key", "tenant_id", "scope", "user_id", "domain_id", "conversation_id", "run_id", "content_json", "source", "evidence_json", "trust_level", "approval_status", "status", "sensitivity", "created_at", "updated_at", "expires_at", "domain_version", "binding_version", "schema_fingerprint", "deduplication_key"}),
        returning=True,
    ),
    "memory:lock_active_slot": _SqlRule(
        "fetchrow", "SELECT", "data_agent_memory_records", _indices(8),
        required_predicates=("owner_key = $1", "deduplication_key = $2", "tenant_id = $3", "scope = $4", "user_id is not distinct from $5", "domain_id is not distinct from $6", "conversation_id is not distinct from $7", "run_id is not distinct from $8"),
        for_update=True,
    ),
    "memory:load_checkpoint": _SqlRule(
        "fetchrow", "SELECT", "data_agent_checkpoints", _indices(6),
        required_predicates=("tenant_id = $1", "user_id = $2", "domain_id = $3", "conversation_id = $4", "run_id = $5", "owner_key = $6"),
    ),
    "memory:recall": _SqlRule(
        "fetch", "SELECT", "data_agent_memory_records", _indices(12),
        required_predicates=("tenant_id = $1", "user_id = $2", "domain_id = $3", "scope = any", "owner_key = any"),
    ),
    "memory:list_messages": _SqlRule(
        "fetch", "SELECT", "data_agent_messages", _indices(5),
        required_predicates=("tenant_id = $1", "user_id = $2", "domain_id = $3", "conversation_id = $4"),
        joins=("data_agent_conversations",),
    ),
    "memory:insert_message": _SqlRule(
        "execute", "INSERT", "data_agent_messages", _indices(11),
        frozenset({"message_id", "tenant_id", "user_id", "domain_id", "conversation_id", "run_id", "owner_key", "role", "content", "safe_payload", "created_at"}),
    ),
    "memory:insert_artifact": _SqlRule(
        "execute", "INSERT", "data_agent_artifact_refs", _indices(11),
        frozenset({"artifact_id", "tenant_id", "user_id", "domain_id", "conversation_id", "run_id", "owner_key", "kind", "digest", "row_count", "created_at"}),
    ),
    "memory:update_conversation": _SqlRule(
        "execute", "UPDATE", "data_agent_conversations", _indices(8),
        assignments=frozenset({"summary", "summary_run_id", "updated_at"}),
        required_predicates=("tenant_id = $1", "user_id = $2", "domain_id = $3", "conversation_id = $4", "owner_key = $5"),
    ),
    "memory:update_proposal_decision": _SqlRule(
        "execute", "UPDATE", "data_agent_memory_proposals", _indices(9),
        assignments=frozenset({"status", "approver_user_id", "approver_roles", "approval_decision", "approval_reason", "decided_at", "updated_at"}),
        required_predicates=("proposal_id = $1", "tenant_id = $8", "owner_key = $9"),
    ),
    "memory:update_proposal_committed": _SqlRule(
        "execute", "UPDATE", "data_agent_memory_proposals", _indices(10),
        assignments=frozenset({"status", "committed_memory_id", "approver_user_id", "approver_roles", "approval_decision", "approval_reason", "decided_at", "updated_at"}),
        required_predicates=("proposal_id = $1", "tenant_id = $9", "owner_key = $10"),
    ),
    "memory:update_proposal_conflict": _SqlRule(
        "execute", "UPDATE", "data_agent_memory_proposals", _indices(9),
        assignments=frozenset({"status", "conflict_with", "approver_user_id", "approver_roles", "approval_decision", "decided_at", "updated_at"}),
        required_predicates=("proposal_id = $1", "tenant_id = $8", "owner_key = $9"),
    ),
    "memory:save_checkpoint": _SqlRule(
        "execute", "INSERT", "data_agent_checkpoints", _indices(10),
        frozenset({"tenant_id", "user_id", "domain_id", "conversation_id", "run_id", "owner_key", "checkpoint_id", "checkpoint_digest", "checkpoint_json", "created_at", "updated_at"}),
    ),
    "memory:mark_version_drift": _SqlRule(
        "execute", "UPDATE", "data_agent_memory_records", _indices(11),
        assignments=frozenset({"status", "updated_at", "invalidation_reason"}),
        required_predicates=("tenant_id = $1", "user_id = $2", "domain_id = $3", "owner_key = any"),
    ),
    "memory:invalidate": _SqlRule(
        "execute", "UPDATE", "data_agent_memory_records", _indices(10),
        assignments=frozenset({"status", "updated_at", "invalidated_at", "invalidation_reason"}),
        required_predicates=("tenant_id = $1",),
    ),
    "memory:forget_unlink_proposals": _SqlRule(
        "execute", "UPDATE", "data_agent_memory_proposals", _indices(5),
        assignments=frozenset({"committed_memory_id", "updated_at"}),
        required_predicates=("tenant_id = $1", "domain_id = $2", "user_id = $3", "conversation_id = $4", "run_id = $5"),
    ),
    "memory:forget_records": _SqlRule(
        "execute", "DELETE", "data_agent_memory_records", _indices(5),
        required_predicates=("tenant_id = $1", "domain_id = $2", "user_id = $3", "conversation_id = $4", "run_id = $5"),
    ),
    "memory:forget_proposals": _SqlRule(
        "execute", "DELETE", "data_agent_memory_proposals", _indices(5),
        required_predicates=("tenant_id = $1", "domain_id = $2", "user_id = $3", "conversation_id = $4", "run_id = $5"),
    ),
    "memory:forget_messages": _SqlRule(
        "execute", "DELETE", "data_agent_messages", _indices(5),
        required_predicates=("tenant_id = $1", "domain_id = $2", "user_id = $3", "conversation_id = $4", "run_id = $5"),
    ),
    "memory:forget_artifacts": _SqlRule(
        "execute", "DELETE", "data_agent_artifact_refs", _indices(5),
        required_predicates=("tenant_id = $1", "domain_id = $2", "user_id = $3", "conversation_id = $4", "run_id = $5"),
    ),
    "memory:forget_checkpoints": _SqlRule(
        "execute", "DELETE", "data_agent_checkpoints", _indices(5),
        required_predicates=("tenant_id = $1", "domain_id = $2", "user_id = $3", "conversation_id = $4", "run_id = $5"),
    ),
    "memory:forget_run_summary": _SqlRule(
        "execute", "UPDATE", "data_agent_conversations", _indices(5),
        assignments=frozenset({"summary", "summary_run_id", "updated_at"}),
        required_predicates=("tenant_id = $1", "domain_id = $2", "user_id = $3", "conversation_id = $4", "summary_run_id = $5"),
    ),
    "memory:forget_conversations": _SqlRule(
        "execute", "DELETE", "data_agent_conversations", _indices(4),
        required_predicates=("tenant_id = $1", "domain_id = $2", "user_id = $3", "conversation_id = $4"),
    ),
}


def _operation_for_sql(sql: str, api: str) -> str:
    shape = _parse_sql_shape(sql)
    candidates: list[tuple[str, _SqlRule]] = []
    for operation, rule in _SQL_RULES.items():
        if (rule.api, rule.kind, rule.table) != (api, shape.kind, shape.table):
            continue
        if rule.placeholders != shape.placeholders:
            continue
        if rule.columns != shape.columns:
            continue
        if rule.assignments != shape.assignments:
            continue
        if rule.returning != shape.returning or rule.for_update != shape.for_update:
            continue
        if rule.joins != shape.joins:
            continue
        candidates.append((operation, rule))
    if len(candidates) != 1:
        raise AssertionError(
            "SQL shape is not a unique supported operation: "
            f"api={api} kind={shape.kind} table={shape.table} "
            f"columns={sorted(shape.columns)} assignments={sorted(shape.assignments)} "
            f"placeholders={sorted(shape.placeholders)}"
        )
    operation, rule = candidates[0]
    missing = [
        predicate
        for predicate in rule.required_predicates
        if predicate not in shape.canonical_sql
    ]
    if missing:
        raise AssertionError(
            f"SQL shape for {operation} is missing predicates: {missing}"
        )
    debug_match = _MARKER.search(sql)
    if debug_match is not None and debug_match.group(1) != operation:
        raise AssertionError(
            f"SQL debug marker {debug_match.group(1)} does not match {operation}"
        )
    return operation


class _Database:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.conversations: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.artifacts: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
        self.proposals: dict[str, dict[str, Any]] = {}
        self.proposal_by_digest: dict[tuple[str, str], str] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.record_by_proposal: dict[str, str] = {}
        self.record_by_slot: dict[tuple[str, str], str] = {}
        self.checkpoints: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self.sql_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_exits: list[type[BaseException] | None] = []
        self.fail_marker: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            name: copy.deepcopy(getattr(self, name))
            for name in (
                "conversations",
                "messages",
                "artifacts",
                "proposals",
                "proposal_by_digest",
                "records",
                "record_by_proposal",
                "record_by_slot",
                "checkpoints",
            )
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)

    def validate_constraints(self) -> None:
        for record in self.records.values():
            if record["proposal_id"] not in self.proposals:
                raise RuntimeError("record proposal foreign key violation")
        for proposal in self.proposals.values():
            memory_id = proposal.get("committed_memory_id")
            if memory_id is not None and memory_id not in self.records:
                raise RuntimeError("proposal memory foreign key violation")
            if proposal["scope"] in {"working", "conversation"}:
                key = (
                    proposal["tenant_id"],
                    proposal["user_id"],
                    proposal["domain_id"],
                    proposal["conversation_id"],
                )
                if key not in self.conversations:
                    raise RuntimeError("proposal conversation foreign key violation")
        for collection in (self.messages, self.artifacts.values(), self.checkpoints.values()):
            for row in collection:
                key = (
                    row["tenant_id"],
                    row["user_id"],
                    row["domain_id"],
                    row["conversation_id"],
                )
                if key not in self.conversations:
                    raise RuntimeError("conversation owner foreign key violation")


class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.snapshot: dict[str, Any] | None = None

    async def __aenter__(self) -> "_Transaction":
        await self.connection.database.lock.acquire()
        self.snapshot = self.connection.database.snapshot()
        self.connection.in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        database = self.connection.database
        observed_type = exc_type
        try:
            if exc_type is None:
                try:
                    database.validate_constraints()
                except Exception as constraint_error:
                    observed_type = type(constraint_error)
                    if self.snapshot is not None:
                        database.restore(self.snapshot)
                    raise
            elif self.snapshot is not None:
                database.restore(self.snapshot)
            return False
        finally:
            database.transaction_exits.append(observed_type)
            self.connection.in_transaction = False
            database.lock.release()


class _Connection:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.in_transaction = False

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def _record(self, sql: str, args: tuple[Any, ...], api: str) -> str:
        operation = _operation_for_sql(sql, api)
        self.database.sql_calls.append((sql, args))
        if self.database.fail_marker == operation:
            raise RuntimeError(f"injected failure at {operation}")
        return operation

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        marker = self._record(sql, args, "fetchrow")
        db = self.database
        if marker == "memory:create_conversation":
            (
                tenant_id,
                user_id,
                domain_id,
                conversation_id,
                owner_key,
                title,
                status,
                created_at,
            ) = args
            row = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "domain_id": domain_id,
                "conversation_id": conversation_id,
                "owner_key": owner_key,
                "title": title,
                "summary": "",
                "summary_run_id": None,
                "status": status,
                "created_at": created_at,
                "updated_at": created_at,
            }
            db.conversations[(tenant_id, user_id, domain_id, conversation_id)] = row
            return copy.deepcopy(row)
        if marker in {"memory:get_conversation", "memory:lock_conversation"}:
            row = db.conversations.get(tuple(args[:4]))
            if row is None or row["owner_key"] != args[4]:
                return None
            return copy.deepcopy(row)
        if marker == "memory:insert_proposal":
            (
                proposal_id,
                candidate_digest,
                owner_key,
                tenant_id,
                scope,
                user_id,
                domain_id,
                conversation_id,
                run_id,
                candidate_json,
                deduplication_key,
                status,
                proposed_at,
            ) = args
            digest_key = (owner_key, candidate_digest)
            existing_id = db.proposal_by_digest.get(digest_key)
            if existing_id is not None:
                return copy.deepcopy(db.proposals[existing_id])
            active_id = db.record_by_slot.get((owner_key, deduplication_key))
            if active_id is not None:
                active_content = json.loads(db.records[active_id]["content_json"])
                candidate_content = json.loads(candidate_json)["content"]
                if active_content != candidate_content:
                    status = "conflict"
            row = {
                "proposal_id": proposal_id,
                "candidate_digest": candidate_digest,
                "owner_key": owner_key,
                "tenant_id": tenant_id,
                "scope": scope,
                "user_id": user_id,
                "domain_id": domain_id,
                "conversation_id": conversation_id,
                "run_id": run_id,
                "candidate_json": candidate_json,
                "deduplication_key": deduplication_key,
                "status": status,
                "proposed_at": proposed_at,
                "updated_at": proposed_at,
                "approver_user_id": None,
                "approver_roles": None,
                "approval_decision": None,
                "approval_reason": None,
                "decided_at": None,
                "committed_memory_id": None,
            }
            db.proposals[proposal_id] = row
            db.proposal_by_digest[digest_key] = proposal_id
            return copy.deepcopy(row)
        if marker == "memory:lock_proposal":
            proposal_id, tenant_id = args
            row = db.proposals.get(proposal_id)
            if row is None or row["tenant_id"] != tenant_id:
                return None
            return copy.deepcopy(row)
        if marker == "memory:insert_record":
            proposal_id = args[1]
            fields = (
                "memory_id",
                "proposal_id",
                "owner_key",
                "tenant_id",
                "scope",
                "user_id",
                "domain_id",
                "conversation_id",
                "run_id",
                "content_json",
                "source",
                "evidence_json",
                "trust_level",
                "approval_status",
                "status",
                "sensitivity",
                "created_at",
                "expires_at",
                "domain_version",
                "binding_version",
                "schema_fingerprint",
                "deduplication_key",
            )
            row = dict(zip(fields, args, strict=True))
            slot = (row["owner_key"], row["deduplication_key"])
            if slot in db.record_by_slot:
                return None
            row.update(
                updated_at=row["created_at"],
                invalidated_at=None,
                invalidation_reason=None,
            )
            db.records[row["memory_id"]] = row
            db.record_by_proposal[proposal_id] = row["memory_id"]
            db.record_by_slot[slot] = row["memory_id"]
            return {"memory_id": row["memory_id"]}
        if marker == "memory:lock_active_slot":
            owner_key, deduplication_key = args[:2]
            memory_id = db.record_by_slot.get((owner_key, deduplication_key))
            if memory_id is None:
                return None
            row = db.records[memory_id]
            explicit = (
                row["tenant_id"],
                row["scope"],
                row["user_id"],
                row["domain_id"],
                row["conversation_id"],
                row["run_id"],
            )
            if explicit != tuple(args[2:8]):
                return None
            return copy.deepcopy(row)
        if marker == "memory:load_checkpoint":
            row = db.checkpoints.get(tuple(args[:5]))
            if row is None or row["owner_key"] != args[5]:
                return None
            return copy.deepcopy(row)
        raise AssertionError(f"unhandled fetchrow marker {marker}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        marker = self._record(sql, args, "fetch")
        db = self.database
        if marker == "memory:recall":
            (
                tenant_id,
                user_id,
                domain_id,
                conversation_id,
                run_id,
                scopes,
                domain_version,
                binding_version,
                schema_fingerprint,
                as_of,
                limit,
                owner_keys,
            ) = args
            rows = []
            for row in db.records.values():
                if row["tenant_id"] != tenant_id or row["owner_key"] not in owner_keys:
                    continue
                if row["scope"] not in scopes or row["status"] != "active":
                    continue
                if row["approval_status"] != "committed":
                    continue
                if row["expires_at"] is not None and row["expires_at"] <= as_of:
                    continue
                pins = (
                    ("domain_version", domain_version),
                    ("binding_version", binding_version),
                    ("schema_fingerprint", schema_fingerprint),
                )
                if any(row[name] is not None and row[name] != value for name, value in pins):
                    continue
                if row["scope"] == "working" and (
                    row["user_id"], row["domain_id"], row["conversation_id"], row["run_id"]
                ) != (user_id, domain_id, conversation_id, run_id):
                    continue
                if row["scope"] == "conversation" and (
                    row["user_id"], row["domain_id"], row["conversation_id"]
                ) != (user_id, domain_id, conversation_id):
                    continue
                if row["scope"] == "user" and row["user_id"] != user_id:
                    continue
                if row["scope"] in {"episodic", "enterprise"} and row["domain_id"] != domain_id:
                    continue
                rows.append(copy.deepcopy(row))
            return rows[:limit]
        if marker == "memory:list_messages":
            tenant_id, user_id, domain_id, conversation_id, limit = args
            rows = [
                copy.deepcopy(row)
                for row in db.messages
                if row["tenant_id"] == tenant_id
                and row["user_id"] == user_id
                and row["domain_id"] == domain_id
                and row["conversation_id"] == conversation_id
            ]
            return rows[-limit:]
        raise AssertionError(f"unhandled fetch marker {marker}")

    async def execute(self, sql: str, *args: Any) -> str:
        marker = self._record(sql, args, "execute")
        db = self.database
        if marker == "memory:insert_message":
            fields = (
                "message_id",
                "tenant_id",
                "user_id",
                "domain_id",
                "conversation_id",
                "run_id",
                "owner_key",
                "role",
                "content",
                "safe_payload",
                "created_at",
            )
            message = dict(zip(fields, args, strict=True))
            if not any(item["message_id"] == message["message_id"] for item in db.messages):
                db.messages.append(message)
            return "INSERT 0 1"
        if marker == "memory:insert_artifact":
            fields = (
                "artifact_id",
                "tenant_id",
                "user_id",
                "domain_id",
                "conversation_id",
                "run_id",
                "owner_key",
                "kind",
                "digest",
                "row_count",
                "created_at",
            )
            artifact = dict(zip(fields, args, strict=True))
            key = (
                artifact["tenant_id"],
                artifact["user_id"],
                artifact["domain_id"],
                artifact["conversation_id"],
                artifact["run_id"],
                artifact["artifact_id"],
            )
            db.artifacts[key] = artifact
            return "INSERT 0 1"
        if marker == "memory:update_conversation":
            (
                tenant_id,
                user_id,
                domain_id,
                conversation_id,
                owner_key,
                summary,
                summary_run_id,
                updated_at,
            ) = args
            row = db.conversations.get((tenant_id, user_id, domain_id, conversation_id))
            if row is None or row["owner_key"] != owner_key:
                return "UPDATE 0"
            row["summary"] = summary
            row["summary_run_id"] = summary_run_id
            row["updated_at"] = updated_at
            return "UPDATE 1"
        if marker == "memory:update_proposal_decision":
            (
                proposal_id,
                status,
                approver,
                roles_json,
                decision,
                reason,
                decided_at,
                tenant_id,
                owner_key,
            ) = args
            row = db.proposals[proposal_id]
            if row["tenant_id"] != tenant_id or row["owner_key"] != owner_key:
                return "UPDATE 0"
            row.update(
                status=status,
                approver_user_id=approver,
                approver_roles=roles_json,
                approval_decision=decision,
                approval_reason=reason,
                decided_at=decided_at,
                updated_at=decided_at,
            )
            return "UPDATE 1"
        if marker == "memory:update_proposal_committed":
            (
                proposal_id,
                status,
                memory_id,
                approver,
                roles_json,
                decision,
                reason,
                decided_at,
                tenant_id,
                owner_key,
            ) = args
            row = db.proposals[proposal_id]
            if row["tenant_id"] != tenant_id or row["owner_key"] != owner_key:
                return "UPDATE 0"
            row.update(
                status=status,
                committed_memory_id=memory_id,
                approver_user_id=approver,
                approver_roles=roles_json,
                approval_decision=decision,
                approval_reason=reason,
                decided_at=decided_at,
                updated_at=decided_at,
            )
            return "UPDATE 1"
        if marker == "memory:update_proposal_conflict":
            (
                proposal_id,
                status,
                conflict_json,
                approver,
                roles_json,
                decision,
                decided_at,
                tenant_id,
                owner_key,
            ) = args
            row = db.proposals[proposal_id]
            if row["tenant_id"] != tenant_id or row["owner_key"] != owner_key:
                return "UPDATE 0"
            row.update(
                status=status,
                conflict_with=json.loads(conflict_json),
                approver_user_id=approver,
                approver_roles=roles_json,
                approval_decision=decision,
                decided_at=decided_at,
                updated_at=decided_at,
            )
            return "UPDATE 1"
        if marker == "memory:save_checkpoint":
            fields = (
                "tenant_id",
                "user_id",
                "domain_id",
                "conversation_id",
                "run_id",
                "owner_key",
                "checkpoint_id",
                "checkpoint_digest",
                "checkpoint_json",
                "created_at",
            )
            row = dict(zip(fields, args, strict=True))
            row["updated_at"] = row["created_at"]
            db.checkpoints[tuple(args[:5])] = row
            return "INSERT 0 1"
        if marker == "memory:mark_version_drift":
            owner_keys = set(args[10])
            changed = 0
            for row in db.records.values():
                if row["owner_key"] not in owner_keys or row["status"] != "active":
                    continue
                pins = (
                    ("domain_version", args[6]),
                    ("binding_version", args[7]),
                    ("schema_fingerprint", args[8]),
                )
                if any(row[name] is not None and row[name] != value for name, value in pins):
                    row["status"] = "pending_review"
                    row["updated_at"] = args[9]
                    row["invalidation_reason"] = "version_drift"
                    changed += 1
            return f"UPDATE {changed}"
        if marker == "memory:forget_unlink_proposals":
            changed = 0
            for row in db.proposals.values():
                if _row_matches_subject(row, args):
                    row["committed_memory_id"] = None
                    changed += 1
            return f"UPDATE {changed}"
        if marker == "memory:forget_records":
            removed = [
                memory_id
                for memory_id, row in db.records.items()
                if _row_matches_subject(row, args)
            ]
            for memory_id in removed:
                row = db.records.pop(memory_id)
                db.record_by_proposal.pop(row["proposal_id"], None)
                db.record_by_slot.pop(
                    (row["owner_key"], row["deduplication_key"]),
                    None,
                )
            return f"DELETE {len(removed)}"
        if marker == "memory:forget_proposals":
            removed = [
                proposal_id
                for proposal_id, row in db.proposals.items()
                if _row_matches_subject(row, args)
            ]
            for proposal_id in removed:
                row = db.proposals.pop(proposal_id)
                db.proposal_by_digest.pop(
                    (row["owner_key"], row["candidate_digest"]),
                    None,
                )
            return f"DELETE {len(removed)}"
        if marker == "memory:forget_messages":
            retained = [row for row in db.messages if not _row_matches_subject(row, args)]
            count = len(db.messages) - len(retained)
            db.messages = retained
            return f"DELETE {count}"
        if marker == "memory:forget_artifacts":
            removed = [
                key
                for key, row in db.artifacts.items()
                if _row_matches_subject(row, args)
            ]
            for key in removed:
                db.artifacts.pop(key)
            return f"DELETE {len(removed)}"
        if marker == "memory:forget_checkpoints":
            removed = [
                key
                for key, row in db.checkpoints.items()
                if _row_matches_subject(row, args)
            ]
            for key in removed:
                db.checkpoints.pop(key)
            return f"DELETE {len(removed)}"
        if marker == "memory:forget_run_summary":
            tenant_id, domain_id, user_id, conversation_id, run_id = args
            row = db.conversations.get(
                (tenant_id, user_id, domain_id, conversation_id)
            )
            if row is None or row.get("summary_run_id") != run_id:
                return "UPDATE 0"
            row["summary"] = ""
            row["summary_run_id"] = None
            return "UPDATE 1"
        if marker == "memory:forget_conversations":
            tenant_id, domain_id, user_id, conversation_id = args
            removed = [
                key
                for key, row in db.conversations.items()
                if row["tenant_id"] == tenant_id
                and row["domain_id"] == domain_id
                and row["user_id"] == user_id
                and (
                    conversation_id is None
                    or row["conversation_id"] == conversation_id
                )
            ]
            for key in removed:
                db.conversations.pop(key)
            return f"DELETE {len(removed)}"
        raise AssertionError(f"unhandled execute marker {marker}")


def _row_matches_subject(row: dict[str, Any], args: tuple[Any, ...]) -> bool:
    tenant_id, domain_id, user_id, conversation_id, run_id = args
    return (
        row.get("tenant_id") == tenant_id
        and row.get("domain_id") == domain_id
        and row.get("user_id") == user_id
        and (
            conversation_id is None
            or row.get("conversation_id") == conversation_id
        )
        and (run_id is None or row.get("run_id") == run_id)
    )


class _Acquire:
    def __init__(self, pool: "_Pool") -> None:
        self.pool = pool
        self.connection = _Connection(pool.database)

    async def __aenter__(self) -> _Connection:
        self.pool.acquired += 1
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        self.pool.released += 1
        return False


class _Pool:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.acquired = 0
        self.released = 0

    def acquire(self) -> _Acquire:
        return _Acquire(self)


def checkpoint(run_id: str = "run-a") -> ExecutionCheckpoint:
    pins = ExecutionVersionPins(
        authority=ExecutionAuthorityPin(
            tenant_id="tenant-a",
            user_id="user-a",
            normalized_roles=("seller",),
            admin_scope=False,
            enterprise_id="olist",
            domain_id="commerce",
            request_id=run_id,
            question_digest="0" * 64,
        ),
        bundle_digest="bundle-a",
        bundle_runtime_version="1.0.0",
        schema_fingerprint="schema-a",
        skill_id="commerce.analytics",
        skill_version="1.0.0",
        graph_id="commerce.execution",
        graph_version="1.0.0",
        graph_digest="graph-a",
        tool_registry_version="1.0.0",
        tool_versions=(VersionPin(component="semantic.search", version="1.0.0"),),
        model_versions=(VersionPin(component="planner", version="model-a"),),
    )
    state = ExecutionState(
        run_id=run_id,
        mode=AgentMode.PLAN,
        status=ExecutionStatus.PAUSED,
        next_node="plan_query",
    )
    return ExecutionCheckpoint.capture(pins=pins, state=state)


def candidate(
    *,
    user_id: str = "user-a",
    value: str = "concise",
    deduplication_key: str | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        owner=UserMemoryOwner(tenant_id="tenant-a", user_id=user_id),
        content=UserMemoryContent(
            preference_key="report_style",
            preference_value=value,
        ),
        source="explicit_user_instruction",
        deduplication_key=deduplication_key,
    )


def approval(
    user_id: str = "user-a",
    *,
    tenant_id: str = "tenant-a",
    decision: ApprovalDecision = ApprovalDecision.APPROVE,
    roles: tuple[str, ...] = (),
) -> ApprovalContext:
    return ApprovalContext(
        tenant_id=tenant_id,
        approver_user_id=user_id,
        roles=roles,
        decision=decision,
        decided_at=NOW,
    )


def turn_batch(
    conversation,
    *,
    run_id: str,
    proposal: MemoryCandidate | None = None,
) -> ConversationWriteBatch:
    reference = ArtifactReference(
        artifact_id=f"query-result:{conversation.conversation_id}:{run_id}",
        tenant_id=conversation.tenant_id,
        user_id=conversation.user_id,
        domain_id=conversation.domain_id,
        conversation_id=conversation.conversation_id,
        run_id=run_id,
        kind="query_result",
        digest=("a" if run_id.endswith("a") else "b") * 64,
        row_count=2,
    )
    owner = {
        "tenant_id": conversation.tenant_id,
        "user_id": conversation.user_id,
        "domain_id": conversation.domain_id,
        "conversation_id": conversation.conversation_id,
        "run_id": run_id,
    }
    return ConversationWriteBatch(
        **owner,
        user_message=MessageWrite(
            **owner,
            role=MessageRole.USER,
            content=f"question {run_id}",
        ),
        assistant_message=MessageWrite(
            **owner,
            role=MessageRole.ASSISTANT,
            content=f"answer {run_id}",
            payload=SafeMessagePayload(artifact_refs=(reference,), row_count=2),
        ),
        conversation_summary=ConversationSummaryWrite(
            **owner,
            summary=f"summary {run_id}",
        ),
        artifact_refs=(reference,),
        proposals=(proposal,) if proposal is not None else (),
        checkpoint=Checkpoint(**owner, checkpoint=checkpoint(run_id)),
    )


class MemoryDDLTests(unittest.TestCase):
    def test_ddl_has_owner_fks_uniques_status_checks_and_indexes(self) -> None:
        statements = sqlglot.parse(
            (PROJECT_ROOT / "db" / "data_agent_memory.sql").read_text(
                encoding="utf-8"
            ),
            read="postgres",
        )
        table_creates = {
            statement.this.this.name: statement
            for statement in statements
            if isinstance(statement, exp.Create)
            and statement.args.get("kind") == "TABLE"
            and isinstance(statement.this, exp.Schema)
        }
        expected_tables = {
            "data_agent_conversations",
            "data_agent_messages",
            "data_agent_artifact_refs",
            "data_agent_memory_proposals",
            "data_agent_memory_records",
            "data_agent_checkpoints",
        }
        self.assertEqual(set(table_creates), expected_tables)

        expected_columns = {
            "data_agent_conversations": {
                "tenant_id", "user_id", "domain_id", "conversation_id",
                "owner_key", "summary", "summary_run_id", "status",
            },
            "data_agent_messages": {
                "message_id", "tenant_id", "user_id", "domain_id",
                "conversation_id", "run_id", "owner_key", "safe_payload",
            },
            "data_agent_artifact_refs": {
                "artifact_id", "tenant_id", "user_id", "domain_id",
                "conversation_id", "run_id", "owner_key", "digest",
            },
            "data_agent_memory_proposals": {
                "proposal_id", "owner_key", "tenant_id", "scope", "user_id",
                "domain_id", "conversation_id", "run_id", "candidate_json",
                "deduplication_key", "status", "committed_memory_id",
            },
            "data_agent_memory_records": {
                "memory_id", "proposal_id", "owner_key", "tenant_id", "scope",
                "user_id", "domain_id", "conversation_id", "run_id",
                "content_json", "deduplication_key", "status",
            },
            "data_agent_checkpoints": {
                "tenant_id", "user_id", "domain_id", "conversation_id",
                "run_id", "owner_key", "checkpoint_id", "checkpoint_json",
            },
        }
        for table, expected in expected_columns.items():
            columns = {
                item.this.name
                for item in table_creates[table].this.expressions
                if isinstance(item, exp.ColumnDef)
            }
            self.assertLessEqual(expected, columns, table)

        def constraint_columns(item: exp.Expression) -> tuple[str, ...]:
            schema = item.this
            values = schema.expressions if isinstance(schema, exp.Schema) else ()
            return tuple(value.name for value in values)

        unique_shapes: dict[str, set[tuple[str, ...]]] = {}
        check_counts: dict[str, int] = {}
        foreign_keys: set[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = set()
        for table, create in table_creates.items():
            unique_shapes[table] = {
                constraint_columns(item)
                for item in create.this.expressions
                if isinstance(item, exp.UniqueColumnConstraint)
            }
            check_counts[table] = sum(
                isinstance(item, exp.CheckColumnConstraint)
                for item in create.this.expressions
            )
            for item in create.this.expressions:
                if not isinstance(item, exp.ForeignKey):
                    continue
                reference = item.args["reference"].this
                foreign_keys.add(
                    (
                        table,
                        tuple(value.name for value in item.expressions),
                        reference.this.name,
                        tuple(value.name for value in reference.expressions),
                    )
                )

        conversation_owner = ("tenant_id", "user_id", "domain_id", "conversation_id")
        for table in (
            "data_agent_messages",
            "data_agent_artifact_refs",
            "data_agent_memory_proposals",
            "data_agent_memory_records",
            "data_agent_checkpoints",
        ):
            self.assertIn(
                (table, conversation_owner, "data_agent_conversations", conversation_owner),
                foreign_keys,
            )
        self.assertIn(
            ("owner_key", "candidate_digest"),
            unique_shapes["data_agent_memory_proposals"],
        )
        self.assertIn(
            ("proposal_id",), unique_shapes["data_agent_memory_records"]
        )
        self.assertIn(("owner_key",), unique_shapes["data_agent_checkpoints"])
        self.assertTrue(all(count > 0 for count in check_counts.values()))

        indexes = {
            statement.this.this.name: statement
            for statement in statements
            if isinstance(statement, exp.Create)
            and statement.args.get("kind") == "INDEX"
            and isinstance(statement.this, exp.Index)
        }
        expected_indexes = {
            "idx_data_agent_conversations_owner_updated",
            "idx_data_agent_messages_owner_created",
            "idx_data_agent_artifact_refs_owner_run",
            "idx_data_agent_memory_proposals_owner_status",
            "uq_data_agent_memory_records_active_dedup",
            "idx_data_agent_checkpoints_owner_updated",
        }
        self.assertLessEqual(expected_indexes, set(indexes))
        active_dedup = indexes["uq_data_agent_memory_records_active_dedup"]
        self.assertTrue(active_dedup.args.get("unique"))
        self.assertEqual(
            active_dedup.this.args["table"].name,
            "data_agent_memory_records",
        )


class PostgresMemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = _Database()
        self.pool = _Pool(self.database)
        self.manager = PostgresMemoryManager(self.pool, clock=lambda: NOW)

    async def _conversation(self):
        return await self.manager.create_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            title="Weekly sales",
        )

    async def test_turn_is_one_transaction_and_rolls_back_without_full_results(self) -> None:
        conversation = await self._conversation()
        reference = ArtifactReference(
            artifact_id="query-result:abc",
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=conversation.conversation_id,
            run_id="run-a",
            kind="query_result",
            digest="a" * 64,
            row_count=1000,
        )
        batch = ConversationWriteBatch(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=conversation.conversation_id,
            run_id="run-a",
            user_message=MessageWrite(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                run_id="run-a",
                role=MessageRole.USER,
                content="Weekly sales?",
            ),
            assistant_message=MessageWrite(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                run_id="run-a",
                role=MessageRole.ASSISTANT,
                content="Sales increased.",
                payload=SafeMessagePayload(
                    answer_summary="Sales increased.",
                    ok=True,
                    row_count=1000,
                    artifact_refs=(reference,),
                ),
            ),
            conversation_summary=ConversationSummaryWrite(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                run_id="run-a",
                summary="Weekly sales analysis.",
            ),
            artifact_refs=(reference,),
            proposals=(candidate(),),
            checkpoint=Checkpoint(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                run_id="run-a",
                checkpoint=checkpoint(),
            ),
        )
        before = self.database.snapshot()
        self.database.fail_marker = "memory:insert_artifact"

        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            await self.manager.save_turn(batch)

        self.assertEqual(self.database.snapshot(), before)
        self.assertIs(self.database.transaction_exits[-1], RuntimeError)
        self.assertEqual(self.pool.acquired, self.pool.released)

        self.database.fail_marker = None
        await self.manager.save_turn(batch)

        self.assertIsNone(self.database.transaction_exits[-1])
        self.assertEqual(len(self.database.messages), 2)
        self.assertEqual(len(self.database.artifacts), 1)
        self.assertEqual(len(self.database.proposals), 1)
        self.assertEqual(len(self.database.checkpoints), 1)
        stored = repr(self.database.snapshot())
        self.assertNotIn("FULL_RESULT_SENTINEL", stored)
        self.assertNotIn('"rows"', stored)
        self.assertIn("query-result:abc", stored)
        self.assertTrue(
            all("$1" in sql for sql, args in self.database.sql_calls if args),
            self.database.sql_calls,
        )

    async def test_checkpoint_round_trip_and_all_owner_dimensions_fail_closed(self) -> None:
        conversation = await self._conversation()
        original = checkpoint("run-a")
        state = Checkpoint(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=conversation.conversation_id,
            run_id="run-a",
            checkpoint=original,
        )

        await self.manager.save_checkpoint("run-a", state)

        restored = await self.manager.load_checkpoint(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=conversation.conversation_id,
            run_id="run-a",
        )
        self.assertEqual(restored, original)
        for changed in (
            {"tenant_id": "tenant-b"},
            {"user_id": "user-b"},
            {"domain_id": "other-domain"},
            {"conversation_id": "conversation-b"},
            {"run_id": "run-b"},
        ):
            owner = {
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "domain_id": "commerce",
                "conversation_id": conversation.conversation_id,
                "run_id": "run-a",
            }
            owner.update(changed)
            self.assertIsNone(await self.manager.load_checkpoint(**owner))

    async def test_concurrent_dedup_and_commit_are_atomic_and_unapproved_is_hidden(self) -> None:
        first, second = await asyncio.gather(
            self.manager.propose(candidate()),
            self.manager.propose(candidate()),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.database.proposals), 1)

        query = MemoryQuery(
            tenant_id="tenant-a",
            user_id="user-a",
            scopes=(MemoryScope.USER,),
            query="report",
            as_of=NOW,
        )
        self.assertEqual(
            (await self.manager.recall(query, MemoryBudget())).records,
            (),
        )
        with self.assertRaises(MemoryApprovalError):
            await self.manager.commit(first, approval("user-b"))

        await asyncio.gather(
            self.manager.commit(first, approval()),
            self.manager.commit(first, approval()),
        )

        self.assertEqual(len(self.database.records), 1)
        recalled = await self.manager.recall(query, MemoryBudget())
        self.assertEqual(len(recalled.records), 1)
        cross_tenant = query.model_copy(update={"tenant_id": "tenant-b"})
        cross_user = query.model_copy(update={"user_id": "user-b"})
        self.assertEqual(
            (await self.manager.recall(cross_tenant, MemoryBudget())).records,
            (),
        )
        self.assertEqual(
            (await self.manager.recall(cross_user, MemoryBudget())).records,
            (),
        )
        sql = "\n".join(item[0] for item in self.database.sql_calls)
        self.assertIn("FOR UPDATE", sql.upper())
        self.assertIn("ON CONFLICT", sql.upper())

    async def test_cross_owner_turn_and_messages_are_not_disclosed(self) -> None:
        conversation = await self._conversation()
        wrong = ConversationWriteBatch(
            tenant_id="tenant-a",
            user_id="user-b",
            domain_id="commerce",
            conversation_id=conversation.conversation_id,
            run_id="run-b",
            user_message=MessageWrite(
                tenant_id="tenant-a",
                user_id="user-b",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                run_id="run-b",
                role=MessageRole.USER,
                content="Private?",
            ),
            assistant_message=MessageWrite(
                tenant_id="tenant-a",
                user_id="user-b",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                run_id="run-b",
                role=MessageRole.ASSISTANT,
                content="No disclosure.",
            ),
            conversation_summary=ConversationSummaryWrite(
                tenant_id="tenant-a",
                user_id="user-b",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                run_id="run-b",
                summary="",
            ),
        )

        with self.assertRaises(PermissionError):
            await self.manager.save_turn(wrong)
        self.assertEqual(
            await self.manager.list_messages(
                tenant_id="tenant-a",
                user_id="user-b",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                limit=10,
            ),
            (),
        )

    async def test_decisions_authenticate_before_terminal_state_or_mutation(self) -> None:
        rejected_id = await self.manager.propose(
            candidate(deduplication_key="reject-slot")
        )
        safe_message = "proposal decision is not authorized or unavailable"
        for context in (
            approval("user-b", decision=ApprovalDecision.REJECT),
            approval(
                "user-a",
                tenant_id="tenant-b",
                decision=ApprovalDecision.REJECT,
            ),
        ):
            with self.subTest(context=context):
                with self.assertRaisesRegex(MemoryApprovalError, safe_message):
                    await self.manager.commit(rejected_id, context)
                self.assertEqual(
                    self.database.proposals[rejected_id]["status"],
                    "pending_approval",
                )

        owner_rejection = approval(
            "user-a",
            decision=ApprovalDecision.REJECT,
        )
        await self.manager.commit(rejected_id, owner_rejection)
        await self.manager.commit(rejected_id, owner_rejection)
        with self.assertRaisesRegex(MemoryApprovalError, safe_message):
            await self.manager.commit(rejected_id, approval("user-a"))

        committed_id = await self.manager.propose(
            candidate(value="brief", deduplication_key="commit-slot")
        )
        await self.manager.commit(committed_id, approval("user-a"))
        with self.assertRaisesRegex(MemoryApprovalError, safe_message):
            await self.manager.commit(committed_id, approval("user-b"))
        with self.assertRaisesRegex(MemoryApprovalError, safe_message):
            await self.manager.commit("proposal:missing", approval("user-a"))

        enterprise = MemoryCandidate(
            owner=EnterpriseMemoryOwner(
                tenant_id="tenant-a",
                domain_id="commerce",
            ),
            content=EnterpriseMemoryContent(
                category="metric_rule",
                statement="GMV excludes cancelled orders.",
            ),
            source="curated_review",
        )
        enterprise_id = await self.manager.propose(enterprise)
        with self.assertRaisesRegex(MemoryApprovalError, safe_message):
            await self.manager.commit(
                enterprise_id,
                approval(
                    "user-a",
                    decision=ApprovalDecision.REJECT,
                    roles=("analyst",),
                ),
            )
        admin_rejection = approval(
            "admin-a",
            decision=ApprovalDecision.REJECT,
            roles=("admin",),
        )
        await self.manager.commit(enterprise_id, admin_rejection)

    async def test_defensive_batch_owner_validation_writes_nothing(self) -> None:
        conversation = await self._conversation()
        valid = turn_batch(conversation, run_id="run-a", proposal=candidate())
        cross_owner = candidate(user_id="user-b")
        bypassed = ConversationWriteBatch.model_construct(
            **{
                **valid.model_dump(),
                "user_message": valid.user_message,
                "assistant_message": valid.assistant_message,
                "conversation_summary": valid.conversation_summary,
                "artifact_refs": valid.artifact_refs,
                "proposals": (cross_owner,),
                "checkpoint": valid.checkpoint,
            }
        )
        before = self.database.snapshot()
        acquired = self.pool.acquired

        with self.assertRaisesRegex(PermissionError, "proposal owner"):
            await self.manager.save_turn(bypassed)

        self.assertEqual(self.database.snapshot(), before)
        self.assertEqual(self.pool.acquired, acquired)

        reference = valid.artifact_refs[0].model_copy(update={"user_id": "user-b"})
        bad_message = valid.assistant_message.model_copy(
            update={
                "payload": valid.assistant_message.payload.model_copy(
                    update={"artifact_refs": (reference,)}
                )
            }
        )
        nested_bypass = ConversationWriteBatch.model_construct(
            **{
                **valid.model_dump(),
                "user_message": valid.user_message,
                "assistant_message": bad_message,
                "conversation_summary": valid.conversation_summary,
                "artifact_refs": (),
                "proposals": (),
                "checkpoint": valid.checkpoint,
            }
        )
        with self.assertRaisesRegex(PermissionError, "artifact reference"):
            await self.manager.save_turn(nested_bypass)
        self.assertEqual(self.database.snapshot(), before)
        self.assertEqual(self.pool.acquired, acquired)

    async def test_owner_specific_slots_and_concurrent_conflict_are_stable(self) -> None:
        user_a = await self.manager.propose(
            candidate(user_id="user-a", deduplication_key="shared-slot")
        )
        user_b = await self.manager.propose(
            candidate(user_id="user-b", deduplication_key="shared-slot")
        )
        await self.manager.commit(user_a, approval("user-a"))
        await self.manager.commit(user_b, approval("user-b"))
        self.assertEqual(len(self.database.records), 2)

        first = await self.manager.propose(
            candidate(value="short", deduplication_key="conflict-slot")
        )
        second = await self.manager.propose(
            candidate(value="detailed", deduplication_key="conflict-slot")
        )
        outcomes = await asyncio.gather(
            self.manager.commit(first, approval("user-a")),
            self.manager.commit(second, approval("user-a")),
            return_exceptions=True,
        )
        self.assertEqual(sum(item is None for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, MemoryConflictError) for item in outcomes), 1)
        self.assertEqual(
            {self.database.proposals[first]["status"], self.database.proposals[second]["status"]},
            {"committed", "conflict"},
        )

        conversation_one = await self._conversation()
        conversation_two = await self.manager.create_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            title="Other",
        )
        working_ids = []
        for conversation, run_id in (
            (conversation_one, "run-a"),
            (conversation_one, "run-b"),
            (conversation_two, "run-a"),
        ):
            working = MemoryCandidate(
                owner=WorkingMemoryOwner(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    domain_id="commerce",
                    conversation_id=conversation.conversation_id,
                    run_id=run_id,
                ),
                content=WorkingMemoryContent(summary=f"{conversation.title} {run_id}"),
                source="checkpoint",
                deduplication_key="working-slot",
            )
            working_ids.append(await self.manager.propose(working))
        for proposal_id in working_ids:
            await self.manager.commit(proposal_id, approval("user-a"))

        for domain_id in ("commerce", "finance"):
            enterprise = MemoryCandidate(
                owner=EnterpriseMemoryOwner(
                    tenant_id="tenant-a",
                    domain_id=domain_id,
                ),
                content=EnterpriseMemoryContent(
                    category="term",
                    statement=f"Rule for {domain_id}",
                ),
                source="curated_review",
                deduplication_key="enterprise-slot",
            )
            proposal_id = await self.manager.propose(enterprise)
            await self.manager.commit(
                proposal_id,
                approval("admin-a", roles=("admin",)),
            )
        self.assertEqual(len(self.database.record_by_slot), 8)

    async def test_forget_is_exact_atomic_and_removes_every_surface(self) -> None:
        first = await self._conversation()
        second = await self.manager.create_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            title="Second",
        )
        for conversation, run_id in (
            (first, "run-a"),
            (first, "run-b"),
            (second, "run-a"),
        ):
            working = MemoryCandidate(
                owner=WorkingMemoryOwner(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    domain_id="commerce",
                    conversation_id=conversation.conversation_id,
                    run_id=run_id,
                ),
                content=WorkingMemoryContent(summary=f"memory {run_id}"),
                source="checkpoint",
                deduplication_key=f"slot-{conversation.conversation_id}-{run_id}",
            )
            await self.manager.save_turn(
                turn_batch(conversation, run_id=run_id, proposal=working)
            )
            proposal_id = await self.manager.propose(working)
            await self.manager.commit(proposal_id, approval("user-a"))

        removed = await self.manager.forget(
            SubjectScope(
                tenant_id="tenant-a",
                domain_id="commerce",
                actor_user_id="user-a",
                user_id="user-a",
                conversation_id=first.conversation_id,
                run_id="run-a",
            )
        )
        self.assertEqual(removed, 6)
        self.assertIn(
            ("tenant-a", "user-a", "commerce", first.conversation_id),
            self.database.conversations,
        )
        self.assertTrue(
            all(
                not (
                    row.get("conversation_id") == first.conversation_id
                    and row.get("run_id") == "run-a"
                )
                for collection in (
                    self.database.messages,
                    self.database.artifacts.values(),
                    self.database.checkpoints.values(),
                    self.database.proposals.values(),
                    self.database.records.values(),
                )
                for row in collection
            )
        )

        await self.manager.forget(
            SubjectScope(
                tenant_id="tenant-a",
                domain_id="commerce",
                actor_user_id="user-a",
                user_id="user-a",
                conversation_id=first.conversation_id,
            )
        )
        self.assertTrue(
            all(
                row.get("conversation_id") != first.conversation_id
                for collection in (
                    self.database.conversations.values(),
                    self.database.messages,
                    self.database.artifacts.values(),
                    self.database.checkpoints.values(),
                    self.database.proposals.values(),
                    self.database.records.values(),
                )
                for row in collection
            )
        )
        self.assertIn(
            ("tenant-a", "user-a", "commerce", second.conversation_id),
            self.database.conversations,
        )

        await self.manager.forget(
            SubjectScope(
                tenant_id="tenant-a",
                domain_id="commerce",
                actor_user_id="user-a",
                user_id="user-a",
            )
        )
        self.assertFalse(self.database.conversations)
        self.assertFalse(self.database.messages)
        self.assertFalse(self.database.artifacts)
        self.assertFalse(self.database.checkpoints)
        self.assertFalse(self.database.proposals)
        self.assertFalse(self.database.records)

    async def test_forget_is_domain_exact_and_run_summary_owned(self) -> None:
        target_latest = await self._conversation()
        other_latest = await self.manager.create_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            title="Other latest",
        )
        finance = await self.manager.create_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="finance",
            title="Finance",
        )
        for conversation, run_id in (
            (target_latest, "run-b"),
            (target_latest, "run-a"),
            (other_latest, "run-a"),
            (other_latest, "run-b"),
            (finance, "run-a"),
        ):
            await self.manager.save_turn(turn_batch(conversation, run_id=run_id))

        for conversation in (target_latest, other_latest):
            await self.manager.forget(
                SubjectScope(
                    tenant_id="tenant-a",
                    domain_id="commerce",
                    actor_user_id="user-a",
                    user_id="user-a",
                    conversation_id=conversation.conversation_id,
                    run_id="run-a",
                )
            )

        cleared = await self.manager.get_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=target_latest.conversation_id,
        )
        retained = await self.manager.get_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=other_latest.conversation_id,
        )
        untouched = await self.manager.get_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="finance",
            conversation_id=finance.conversation_id,
        )
        self.assertEqual((cleared.summary, cleared.summary_run_id), ("", None))
        self.assertEqual(
            (retained.summary, retained.summary_run_id),
            ("summary run-b", "run-b"),
        )
        self.assertEqual(
            (untouched.summary, untouched.summary_run_id),
            ("summary run-a", "run-a"),
        )

        await self.manager.forget(
            SubjectScope(
                tenant_id="tenant-a",
                domain_id="commerce",
                actor_user_id="user-a",
                user_id="user-a",
            )
        )
        self.assertTrue(
            all(row["domain_id"] == "finance" for row in self.database.conversations.values())
        )
        for collection in (
            self.database.messages,
            self.database.artifacts.values(),
            self.database.checkpoints.values(),
        ):
            self.assertTrue(all(row["domain_id"] == "finance" for row in collection))

    async def test_sql_shape_carries_owner_key_and_explicit_dimensions(self) -> None:
        conversation = await self._conversation()
        await self.manager.save_turn(turn_batch(conversation, run_id="run-a"))
        await self.manager.list_messages(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=conversation.conversation_id,
            limit=10,
        )
        await self.manager.load_checkpoint(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=conversation.conversation_id,
            run_id="run-a",
        )
        await self.manager.forget(
            SubjectScope(
                tenant_id="tenant-a",
                domain_id="commerce",
                actor_user_id="user-a",
                user_id="user-a",
                conversation_id=conversation.conversation_id,
                run_id="run-a",
            )
        )

        by_marker = {_marker(sql): sql.lower() for sql, _ in self.database.sql_calls}
        for marker in (
            "memory:lock_conversation",
            "memory:update_conversation",
            "memory:list_messages",
            "memory:load_checkpoint",
        ):
            sql = by_marker[marker]
            self.assertIn("owner_key", sql, marker)
            self.assertIn("tenant_id", sql, marker)
            self.assertIn("user_id", sql, marker)
            self.assertIn("conversation_id", sql, marker)
        for marker in (
            "memory:lock_conversation",
            "memory:update_conversation",
            "memory:list_messages",
            "memory:load_checkpoint",
            "memory:forget_unlink_proposals",
            "memory:forget_records",
            "memory:forget_proposals",
            "memory:forget_messages",
            "memory:forget_artifacts",
            "memory:forget_checkpoints",
            "memory:forget_run_summary",
        ):
            sql = by_marker[marker]
            self.assertIn("domain_id", sql, marker)
            self.assertNotIn("domain_id is not null", sql, marker)
        for marker in (
            "memory:load_checkpoint",
            "memory:forget_records",
            "memory:forget_proposals",
            "memory:forget_messages",
            "memory:forget_artifacts",
            "memory:forget_checkpoints",
            "memory:forget_run_summary",
        ):
            self.assertIn("run_id", by_marker[marker], marker)

    async def test_fake_rejects_a_valid_marker_on_invalid_sql_shape(self) -> None:
        connection = _Connection(self.database)
        with self.assertRaisesRegex(AssertionError, "SQL shape"):
            await connection.execute(
                "/* memory:forget_messages */ DELETE 1",
                "tenant-a",
                "commerce",
                "user-a",
                None,
                None,
            )


if __name__ == "__main__":
    unittest.main()
