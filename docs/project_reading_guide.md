# Project Reading Guide

## Start with the product boundary

Read these files first:

1. `src/data_agent/runtime/contracts.py` — public `DataAgentRuntime.run()`.
2. `src/data_agent/runtime/models.py` and `events.py` — request, response,
   principal, modes, trace, version pins, and terminal-event invariants.
3. `src/data_agent/runtime/upload_runtime.py` — the default upload-only
   conversation boundary.
4. `src/data_agent/runtime/service.py` — the optional governed-pack runtime.
5. `src/data_agent/runtime/composition_root.py` — the default upload-only
   composition plus explicit pack/OList compatibility composition.

Product adapters are `src/data_agent/cli.py`, `src/api/`, `main.py`, and
`src/data_agent/execution/langgraph_adapter.py`. They must not construct a
Skill, Tool, Memory provider, or database connector directly.

## Follow the six layers

### 1. Runtime

The default runtime persists identity-scoped conversations and rejects
questions without an activated user datasource. The optional pack runtime
loads and pins a resolved bundle, enforces deadline/budget, drives the graph,
persists a safe turn, and emits exactly one terminal event.

### 2. Skill System

`src/data_agent/skills/` defines typed logical analytics models and the
Commerce validator. Start at `skills/models.py` and
`skills/commerce/analytics.py`. Logical plans use `commerce.*` references and
must not contain database identifiers.

### 3. Execution Graph

`src/data_agent/execution/` owns the single bounded graph specification,
compiler, executor, artifacts, checkpoints, retry routes, and LangGraph
adapter. `executor.py` is the easiest place to follow the node sequence.

### 4. Tool Registry and Connector

`src/data_agent/tools/registry.py` and `invoker.py` enforce manifests,
capabilities, grants, budgets, retries, and redacted trace. The six providers
are composed in `tools/providers/registry.py`. PostgreSQL execution lives in
`tools/connectors/postgres.py` and accepts only compiler-created prepared
queries.

### 5. Memory

`src/data_agent/memory/` defines the provider contract, null/PostgreSQL/graph
providers, policy, conversation writes, approved recall, proposals, and
checkpoints. Memory is context, never configuration authority.

### 6. Enterprise Data Binding

`runtime/binding.py` maps a validated logical plan to OList relations, join
paths, typed parameters, required access, and PostgreSQL SQL AST. Seller scope
is injected deterministically at this layer; admin bypass is configured in the
enterprise pack.

## Read configuration separately from code

- `packs/domains/commerce/`: canonical semantics and 48 evals.
- `packs/enterprises/olist/`: nine physical bindings and access policy.
- `packs/deployments/olist-local.yaml`: selected packs and runtime limits.
- `schema_catalog.json`: physical schema attestation.
- `generated/bundles/olist-local.json`: immutable resolved bundle.
- `generated/semantic/commerce.json`: reproducible offline semantic index.

This separation is the core extension rule: a new enterprise adds a binding;
a new domain adds a domain pack and skill; a new database adds a Connector.

## Trace one request

1. CLI/API creates `AgentRequest` and `PrincipalContext`.
2. Runtime snapshots the active bundle and pins all component versions.
3. Context resolver recalls approved, scoped Memory.
4. `commerce.analytics` produces and validates a `LogicalQueryPlan`.
5. Binding creates `BoundQueryPlan`, required access, and `PreparedQuery`.
6. Tool Invoker grants the exact relation/operation capabilities.
7. Connector performs EXPLAIN/preview/execute according to mode.
8. Evidence is profiled, validated, rendered, and persisted with safe trace.
9. Runtime emits `run_completed` or a safe `run_failed` terminal.

## Tests to read

- `tests/contract/`: pack and public contract boundaries.
- `tests/unit/runtime/`, `unit/skills/`, `unit/execution/`, `unit/tools/`, and
  `unit/memory/`: layer behavior.
- `tests/integration/test_data_agent_runtime.py`: three modes through the real
  graph.
- `tests/e2e/test_olist_golden_runtime.py`: all 48 evals through the same public
  Runtime for seller/admin scope.
- API/OpenAPI/frontend schema tests at `tests/test_*contract.py` and
  `frontend/src/*.test.js`.

Run pack compilation and freshness checks whenever a pack, schema, public API,
or generated frontend contract changes.
