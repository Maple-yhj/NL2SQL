# Data Analysis Agent implementation progress

Last updated: 2026-08-08 (active implementation session)

This is the resumable execution ledger for `docs/superpowers/plans/2026-08-08-data-analysis-agent-implementation.md`. Update it after each Task and again before the execution budget reaches 1%.

## Repository state and protected work

- Active branch: `Agent`.
- Pre-existing user changes are protected in:
  - `frontend/src/relationships/RelationshipGraphEditor.tsx`
  - `frontend/src/relationships/relationshipGraphState.test.ts`
  - `frontend/src/relationships/relationshipGraphState.ts`
  - `frontend/src/styles.css`
- Do not reset, checkout, stash-and-forget, broadly format, or delete those changes.
- Do not open or print `.env`, `.env.codex-backup-*`, or `.kaggle/` contents.
- No commit, branch merge, push, or destructive cleanup has been performed.

## Task status

| Task | Status | Notes |
| --- | --- | --- |
| Task 0 — baseline and removal manifest | completed | Added `docs/data-analysis-agent-baseline.md` and `docs/repository-removal-manifest.md`; all baseline gates passed; Task 0 checkboxes updated. |
| Task 1 — public domain models and state | completed | Added strict models/state/reducers and all design error codes. 26 focused tests passed; 32 passed with existing runtime/import tests. |
| Task 2 — response, pins and typed events | completed | Added Pack/Dataset pin union, response summaries, strict event union, waiting/resumed event-store state, frontend validator/types and regenerated contracts. Backend focused: 24 passed + 3 subtests; frontend: 56 passed; build passed. |
| Task 3 — Artifact Store | completed | Added SQLite metadata + atomic JSON payload store, safe preview/redaction, digest/idempotency/isolation/retention. 8 new tests; Task 1–3 focused 34 passed. |
| Task 4 — deterministic query extraction | completed | Added `data_agent.dataset_query` models/planner/compiler/executor/results; API service is a thin compatibility facade. 65 focused tests + 32 subtests passed. |
| Task 5 — dataset tool authority/registry | completed | Added Pack/Dataset authority envelope, 12-tool dataset registry, artifact-backed providers, strict DSL and security gates. 53 tests + 145 subtests passed. |
| Task 6 — planner/evaluator/synthesizer | completed | Added bounded untrusted-data prompts, strict JSON correction, deterministic evaluation and grounded synthesis. 46 analysis-agent tests passed. |
| Task 7 — native LangGraph graph | completed | Added guarded native StateGraph, dynamic cycles, budgets, typed interrupt and public event stream. 61 analysis-agent tests passed. |
| Task 8 — checkpointer/runtime/composition | completed | Added strict non-pickle InMemory/SQLite/optional Postgres factories, principal/pin-validated runtime, durable resume and opt-in composition root. 70 tests + 2 subtests passed. |
| Task 9 — API resume/default switch | completed | Default FastAPI requests now use the native Agent; normal/stream resume, replay, active/waiting conflict and cancellation are persisted. Focused 58 + 32 subtests; expanded API/runtime 154 + 50 subtests. |
| Task 10 — conversation/memory/evidence persistence | completed | Split SQLiteConversationRepository, moved final-turn writes into the graph, added bounded history/approved-memory recall and exact run idempotency. 58 focused + 20 subtests; expanded 77 + 29 subtests. |
| Task 11 — frontend Agent UX | completed | Added strict event state, run/plan/step/input panels, waiting/resume/cancel/replay UX and multi-artifact evidence. 16 files / 62 tests passed; production build passed. |
| Task 12 — trajectory/security gates | completed | Added fixed cases, reusable invariants, real native trajectories and adversarial gates. Plan suite 17 + 117 subtests; expanded runtime suite 28 + 119 subtests. |
| Task 13 — Studio/CLI/docs/generated contracts | completed | Studio exports the native 13-node graph with a directly runnable inert development context; CLI uses the native runtime; docs/contracts are current. 18 focused tests + 3 subtests, 63 frontend tests and production build passed. |
| Task 14 — proven legacy cleanup | pending | No deletion authorized; all manifest candidates remain `unverified`. |
| Task 15 — final release verification | pending | Not started. |
| Task 16 — merge to local main | pending | Must not start before Tasks 0–15 and clean committed source branch. |

## Completed verification

- Focused backend baseline: 39 passed, 26 subtests passed, 1 warning.
- Full backend baseline: 484 passed, 1 warning in 44.34s.
- Frontend baseline: 13 files / 54 tests passed.
- Frontend production build: passed.
- Wheel baseline: `data_agent-0.1.0-py3-none-any.whl` built successfully.

Exact commands and results are in `docs/data-analysis-agent-baseline.md`.

## Completed Task 1 checkpoint

New tests added:

- `tests/unit/analysis_agent/__init__.py`
- `tests/unit/analysis_agent/test_models.py`
- `tests/unit/analysis_agent/test_state.py`

The implementation and tests freeze these behaviors:

- four immutable datasource/binding pins plus tenant/user/mode/relation allowlist;
- strict frozen Pydantic models and checked `model_construct`;
- revisioned unique-step DAG plans;
- namespaced tool names and recursive rejection of authority, arbitrary SQL/code, credential, DSN, and file-path arguments;
- artifact/evidence digest and observation integrity;
- mutually exclusive planner/evaluator decision fields;
- numeric finding evidence grounding;
- canonical JSON SHA-256 digests;
- append-only reducer order, replay idempotency, conflict detection, input immutability, and revalidation;
- finite Agent status transitions.

Focused result: `26 passed in 0.12s`; expanded compatibility result: `32 passed in 1.00s`.

Implemented files:

- `src/data_agent/analysis_agent/__init__.py`
- `src/data_agent/analysis_agent/models.py`
- `src/data_agent/analysis_agent/state.py`
- `src/data_agent/runtime/errors.py` (all design-specified `AGENT_*` codes)

## Next exact actions (Task 2)

1. Inspect `src/api/schemas.py`, `src/api/run_streams.py`, `frontend/src/types.ts`, `frontend/src/agentResponseValidator.ts`, and typed-event/OpenAPI/schema freshness tests.

   ```text
   .venv/bin/python -m pytest tests/unit/runtime/test_typed_public_events.py tests/test_frontend_agent_response_schema.py tests/test_openapi_contract.py tests/test_run_streams.py -q -p no:cacheprovider
   ```

2. Add failing tests for dataset/pack version-pin discriminator, expanded AgentResponse summaries, strict typed event payloads, and waiting terminal semantics.
3. Implement backend contracts first while preserving legacy constructors through explicit `kind="pack"` defaults/aliases where necessary.
4. Update frontend types/validator and regenerate public schema/OpenAPI artifacts only through repository scripts.
5. Run Task 2 focused backend and frontend verification, then update checkboxes and this ledger.

## Completed Task 2 checkpoint

Implemented or updated:

- dependency-light shared public contracts (`src/data_agent/public_contracts.py`);
- `PackRuntimeVersionPins | DatasetRuntimeVersionPins` discriminated union;
- `AgentResponse` and conversation metadata analysis summaries;
- strict typed event payloads for context, plan, step, tool, observation, waiting, resumed and synthesis;
- RunEventStore sequence idempotency/continuity, waiting status and terminal closure;
- frontend event union, event validator and contract tests;
- OpenAPI, Apifox and generated AgentResponse browser contract.

Verification:

```text
.venv/bin/python -m pytest tests/unit/runtime/test_typed_public_events.py tests/test_frontend_agent_response_schema.py tests/test_openapi_contract.py tests/test_run_streams.py tests/unit/runtime/test_runtime_service.py tests/integration/test_data_agent_runtime.py -q -p no:cacheprovider
# 24 passed, 3 subtests passed
npm --prefix frontend test
# 14 files, 56 tests passed
npm --prefix frontend run build
# passed
```

## Next exact actions (Task 3)

1. Add `tests/unit/analysis_agent/test_artifacts.py` covering JSON digest, repeated writes, tenant/user/run isolation, traversal rejection, tamper detection, atomic failure, safe preview/redaction and retention.
2. Run it once to record the missing implementation failure.
3. Implement `src/data_agent/analysis_agent/artifacts.py` using SQLite metadata and server-generated hashed paths with atomic replace; never pickle.
4. Run the focused Artifact Store test and Task 1/2 regression set.

## Completed Task 3 checkpoint

- Added `src/data_agent/analysis_agent/artifacts.py`.
- Added `tests/unit/analysis_agent/test_artifacts.py` (8 tests).
- Store paths are derived only from owner hashes and generated artifact IDs.
- Metadata and payload integrity are checked independently; replayed calls are idempotent and conflicting call payloads fail closed.
- Verification: `34 passed in 0.19s` for Task 1–3 focused tests.

## Next exact actions (Task 4)

1. Inventory classes/functions/import coupling in `src/api/dataset_query_service.py` and its tests.
2. Create extraction compatibility tests/import assertions before moving definitions.
3. Add `src/data_agent/dataset_query/{models,planner,compiler,executor,results}.py` and keep the API facade thin.
4. Preserve JSON schemas and single-query outputs; run the plan-listed dataset/relationship/connector tests.

## Completed Task 4 checkpoint

- Added `src/data_agent/dataset_query/` with `models.py`, `planner.py`, `compiler.py`, `executor.py`, `results.py` and public exports.
- Replaced `src/api/dataset_query_service.py` with a thin authority-resolution/orchestration facade that delegates all deterministic services.
- Added `tests/unit/dataset_query/test_extraction.py` to assert extracted ownership and pure rendering compatibility.
- Verification: 65 passed, 32 subtests passed across dataset service, extraction, relationships and all three connectors.

## Next exact actions (Task 5)

1. Inspect Tool authority assumptions in `tools/models.py`, `registry.py`, `invoker.py` and legacy provider composition.
2. Add dataset registry/provider security tests first, especially mode, pins, artifact ownership, arbitrary SQL/code and credential-tool denial.
3. Generalize authority through an explicit discriminated envelope while keeping Pack behavior green.

## Completed Task 5 checkpoint

- Generalized `ToolSpec`, `ToolInvocationContext`, registry filtering and `ToolInvoker` around a discriminated Pack/Dataset authority envelope while retaining the Pack compatibility adapter.
- Added an independent 12-tool Dataset Agent Registry for catalog, semantic graph, relationship route, query compile/explain/preview/execute, profiling, restricted computation, chart and evidence operations.
- All provider payloads are strict schemas; source/pins/credentials/raw SQL/code/path overrides are rejected, credential tools are mode-gated, and current-run Artifact Store ownership is enforced.
- Query execution reuses Task 4 compiler/executor and preserves allowed-relation, row-limit and statement-timeout authority.
- Tool traces now include safe argument digests and artifact/evidence IDs; evidence outputs can be validated directly as `AgentObservation` input.
- Schema fingerprints accept both repository-native `sha256:<hex>` and legacy bare hex without weakening result/artifact digest validation.

Verification:

```text
.venv/bin/python -m pytest tests/unit/tools tests/unit/analysis_agent/test_artifacts.py tests/integration/test_file_datasource.py tests/integration/test_sqlite_connector.py tests/integration/test_postgres_connector.py -q -p no:cacheprovider
# 53 passed, 145 subtests passed in 2.36s
```

## Next exact actions (Task 6)

1. Add strict provider-neutral Planner/Evaluator/Synthesizer contracts and bounded correction tests.
2. Build prompt envelopes that include only safe catalog/binding summaries, bounded observations, budgets and allowed tool schema summaries.
3. Add prompt-injection fixtures for cell/column/table/error content, then implement deterministic evidence and answer-grounding validators.

## Completed Task 6 checkpoint

- Added provider-neutral `AnalysisPlanner`, `AnalysisEvaluator`, and `AnalysisSynthesizer` over the existing minimal `ModelClient.complete` protocol.
- Added strict single-object JSON parsing, Pydantic validation, redacted validation summaries, and a maximum of three bounded repair attempts.
- Planner prompts contain only the goal, safe context summaries, current plan, bounded observations, remaining budget and allowed tool name/description/input schema; action schemas and tool allowlists are validated server-side.
- Evaluator runs deterministic tool-error, run/schema/digest, empty-result, non-finite, contradiction and budget checks before consulting a model.
- Synthesizer accepts only current-run validated evidence/artifacts, rejects ungrounded numerical answers and foreign charts, and appends a Preview limitation deterministically.
- Prompt tests cover forged tool JSON, Markdown fences, SQL/instruction text in cells and names, and DSN/path/provider-error redaction under an explicit `untrustedData` envelope.

Verification:

```text
.venv/bin/python -m pytest tests/unit/analysis_agent/test_planner.py tests/unit/analysis_agent/test_evaluator.py tests/unit/analysis_agent -q -p no:cacheprovider
# 46 passed in 0.64s
```

## Next exact actions (Task 7)

1. Define guarded route enums and deterministic budget/deadline/cancellation checks.
2. Implement small state-plus-runtime nodes for initialization, context, planning, tool execution, observation, evaluation, interrupt, synthesis, validation and finalization.
3. Compile the native LangGraph with finite recursion bounds and add one/two-tool, replan, failure, interrupt, mode and exactly-one-terminal tests.

## Completed Task 7 checkpoint

- Added closed server route enums and guards for authority/mode/tool schema, deadline, cancellation, agent/model/tool/query/replan budgets.
- Added small injected-context nodes for initialize, context load, plan/replan, guard, tool, observation, evaluation, interrupt/resume, synthesis, validation, persistence, completion and failure.
- Added a native LangGraph `StateGraph` with a real evaluation-to-planner cycle; `execute_tool` has exactly one inbound edge from `guard_decision`.
- Added `DatasetAgentToolInvoker`, which converts guarded actions into the unified Task 5 Invoker and stable replay-safe call IDs, then emits checkpoint-safe observations.
- Added strict custom/update chunk translation to public `AgentEvent`; internal graph chunks never leave `astream_events`, waiting streams end with typed `run_waiting`, and completed/failed streams carry exactly one terminal response.
- Added deterministic graph ID/version/digest pins and a finite recursion limit derived from the hard Agent budget.
- Fixed native interrupt handling so `GraphInterrupt` propagates to LangGraph instead of being swallowed by safe node error handling.

Verification:

```text
.venv/bin/python -m pytest tests/unit/analysis_agent -q -p no:cacheprovider
# 61 passed in 1.32s
.venv/bin/python -m pytest tests/unit/runtime/test_typed_public_events.py tests/test_frontend_agent_response_schema.py tests/test_openapi_contract.py -q -p no:cacheprovider
# 10 passed in 1.70s
```

## Next exact actions (Task 8)

1. Add checkpointer factories for InMemory and SQLite with explicit setup/close lifecycle and no pickle fallback.
2. Build `DataAnalysisAgentRuntime` around `astream_events`, thread_id=run_id, immutable owner metadata, safe terminal/waiting/error handling and resume validation.
3. Compose datasource context, Dataset Tool Invoker, model components, Artifact Store and version pins without import-time connections; add pause/restart/resume isolation tests.
4. Build the dataset-only registry and providers on extracted services plus Artifact Store.

## Completed Task 8 checkpoint

- Added explicit-lifecycle InMemory, local AsyncSQLite and optional AsyncPostgres checkpointer factories; the serializer disables pickle fallback and allowlists only project-owned checkpoint contract types.
- Added `DataAnalysisAgentRuntime` with `thread_id=run_id`, server-owned tenant/user/conversation metadata, public-only event streaming, exactly one closing event, safe failures and resource closure.
- Added principal-protected checkpoint reads, missing/corrupt/stale interrupt errors, immutable request/authority pin revalidation, per-run resume serialization and waiting cancellation.
- Added `DatasetAnalysisRunResolver` and composition builders for active DataSource bindings, Artifact Store, the 12 governed dataset tools, model components, bounded context and version pins.
- Added an opt-in runtime composition root without changing the FastAPI default in Task 8.
- Fixed a real durability race: the runtime now drains LangGraph after a waiting/terminal event so SQLite writes finish before composition shutdown.
- Tests cover active binding composition, import/lifecycle inertness, runtime-only secret/large-value exclusion, InMemory resume, SQLite close/rebuild/resume, duplicate response, cross-user/cross-tenant, stale pins, corrupt/missing checkpoint, cancelled/completed conflicts and strict serializer behavior.

Verification:

```text
LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest tests/integration/test_analysis_agent_runtime.py tests/integration/test_analysis_agent_resume.py tests/unit/analysis_agent -q -p no:cacheprovider
# 70 passed, 2 subtests passed in 1.89s
```

## Next exact actions (Task 9)

1. Inspect the existing FastAPI lifespan, question/stream/cancel routes, `RunCoordinator`/`RunEventStore`, and request/response OpenAPI contracts.
2. Add failing API tests for normal and streaming resume, replay sequence continuity, waiting cancellation and cross-principal denial.
3. Integrate the native runtime with one owned DataSourceService/model/checkpointer lifecycle and switch the default API factory only after the focused API suite passes.
4. Preserve compatibility response fields while eliminating the old DataSourceQueryService branch from the default request path.

## Completed Task 9 checkpoint

- Default FastAPI composition now owns one conversation shell, DataSourceService, native Analysis Agent/checkpointer and model lifecycle; injected legacy/new runtimes remain supported for isolated tests.
- `/api/nl2sql` and conversation messages, normal and SSE, route directly to `analysis_runtime`; routes contain no DataSourceQueryService planner/compiler/connector branch.
- Added strict normal and streaming resume endpoints with owner/status/interrupt/checkpoint validation and monotonic sequence continuation.
- Non-stream calls now use the same persistent event log as SSE and return a typed HTTP 202 waiting envelope when interrupted.
- RunEventStore uses explicit running/waiting/completed/failed/cancelled states, persists conversation ownership, and rejects a second active/waiting run for the same tenant/user/conversation.
- Active graph cancellation now updates the checkpoint and emits/persists a CANCELLED failure event; waiting cancellation does the same and permanently blocks resume.
- Final dataset responses are reconstructed from validated Artifact Store payloads, preserving dataset logical plan, parameterized SQL, rows and deterministic chart fields without checkpointing large results.
- Added a fixed-fixture legacy-vs-native dual-run gate comparing logical aggregation refs, normalized SQL AST digest, exact rows/chart and the answer's primary value.
- Regenerated the checked-in Apifox/OpenAPI document.

Verification:

```text
LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest tests/test_api_nl2sql.py tests/test_api_conversations.py tests/test_api_resume.py tests/test_api_runtime_contract.py tests/test_run_streams.py tests/test_dataset_query_service.py tests/test_openapi_contract.py tests/integration/test_analysis_agent_runtime.py tests/integration/test_analysis_agent_resume.py -q -p no:cacheprovider
# 58 passed, 32 subtests passed

LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest tests/unit/runtime tests/test_api_auth.py tests/test_api_datasources.py tests/test_api_health.py tests/test_api_nl2sql.py tests/test_api_conversations.py tests/test_api_resume.py tests/test_api_runtime_contract.py tests/test_run_streams.py tests/test_dataset_query_service.py -q -p no:cacheprovider
# 154 passed, 50 subtests passed
```

## Next exact actions (Task 10)

1. Inspect UploadDatasetRuntime conversation tables/write semantics and memory contracts/providers for an idempotent completed-turn persistence boundary.
2. Feed bounded conversation summary and approved memory into AgentContextSnapshot without placing either store/client in checkpoint state.
3. Persist exactly one final turn after successful resume/completion, including analysis plan, steps, pins, artifacts and evidence; never persist a turn for waiting.
4. Add scope, idempotency, bounded-recall and failure-policy tests, then run the Task 10 focused suite.

## Completed Task 10 checkpoint

- Extracted `SQLiteConversationRepository`; `UploadDatasetRuntime` now owns only the no-datasource rejection behavior and delegates the conversation facade.
- Removed API terminal-turn recording. The native graph builds the same final `AgentResponse` at `persist_turn` and delegates the atomic write before completion.
- SQLite conversation turns now persist `run_id` and role with a unique owner/run/role index, exact replay no-op behavior and conflicting replay rejection.
- Assistant history stores plan, completed steps, artifacts, evidence, limitations, version pins and safe trace along with the existing response conveniences.
- Follow-up context is bounded and includes only prior user/assistant text and compact evidence identifiers; SQL, rows, traces and prior run state are excluded. Approved Memory recall is separately bounded and only returns committed records.
- Waiting runs write no final turn; successful resume executes persistence once, while duplicate resume is rejected.
- Null and PostgreSQL Memory turn writes now reject conflicting owned-run replay; exact retries do not duplicate messages. Existing proposal → approval → commit authority remains unchanged, and the Agent composition never calls `commit`.
- LangGraph Checkpointer remains independent from long-term Memory; neither store/client is placed in graph state.

Verification:

```text
LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest tests/test_api_conversations.py tests/unit/memory tests/integration/test_memory_postgres.py tests/integration/test_analysis_agent_runtime.py tests/integration/test_analysis_agent_resume.py -q -p no:cacheprovider
# 58 passed, 20 subtests passed

LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest tests/test_api_conversations.py tests/unit/memory tests/integration/test_memory_postgres.py tests/integration/test_analysis_agent_runtime.py tests/integration/test_analysis_agent_resume.py tests/unit/runtime/test_upload_runtime.py tests/test_api_runtime_contract.py tests/test_api_resume.py -q -p no:cacheprovider
# 77 passed, 29 subtests passed
```

## Next exact actions (Task 11)

1. Inspect the current frontend app/API/type architecture and the protected relationship/styles diffs before editing.
2. Add reducer tests first for sequence gaps, replay idempotency, waiting/resume/cancel and terminal hydration.
3. Implement the Agent run panel, plan/step/input/evidence components and streaming resume API without exposing unsafe payloads.
4. Integrate the panel into the existing result-first UI, append isolated CSS, then run the full Vitest suite and production build.

## Completed Task 11 checkpoint

- Added a strict `AgentEvent` reducer with contiguous-sequence enforcement, exact duplicate idempotency, conflicting replay rejection and deterministic replay hydration.
- Added run, plan, step and input-card components showing plan revision, step status, server-provided business tool labels and bounded observation summaries.
- SSE now treats `run_waiting` as a valid closing event. Both normal and streaming resume API methods are available; the application uses streaming resume and handles repeated waits.
- Active/waiting run IDs are scoped to a conversation in local storage. Conversation reload restores state from the owner-protected event replay endpoint and removes completed/failed records.
- Active and waiting cancellation have explicit controls. A waiting run blocks a second message until resume or cancel.
- The evidence drawer now renders every artifact and evidence summary in addition to the existing SQL/trace/version sections; final answer, chart and table remain the primary response surface.
- No prompt, raw arguments, chain-of-thought, credential, internal path or unredacted payload field is accepted or rendered.
- Added an isolated responsive `agent-*` CSS block after inspecting the protected relationship/style diff; all user relationship editor changes remain present.

Verification:

```text
npm --prefix frontend test
# 16 files, 62 tests passed
npm --prefix frontend run build
# passed (1597 modules transformed)
```

## Next exact actions (Task 12)

1. Inventory existing native Agent graph fixtures and migrate only result-level OList cases needed for stable oracle coverage.
2. Add trajectory fixtures for single-query, multi-query, trend/anomaly, empty-result replan, ambiguity, preview, budget, cancellation and restart/resume.
3. Add adversarial security fixtures for prompt injection, forged tools/authority, artifact isolation, stale interrupts, identifier/provider redaction and checkpoint scans.
4. Assert result/evidence/tool/SQL invariants without requiring one exact natural-language response or action ordering.

## Completed Task 12 checkpoint

- Added `tests/fixtures/analysis_agent_cases.json` and reusable result/trajectory/SQL/evidence assertions in `tests/support/analysis_agent_evaluator.py`.
- Added a real uploaded-dataset native Agent oracle: exact aggregate 35, evidence-backed answer, three necessary governed tool calls, source/binding pins and a read-only allowlisted SQL AST.
- Added native graph trajectories for two-query comparison, query/profile/restricted-compute/chart/evidence trend analysis, and empty-result replan.
- Added schema and relationship ambiguity interrupts with choice-based resume, Preview limitation, finite budget exhaustion, cancellation with no tool call after cancellation, and SQLite restart/resume sequence continuity.
- Added adversarial tests for prompt injection in data/context, unknown tools, raw SQL/code, forged tenant/source authority, artifact cross-tenant/user/run access, stale interrupt/cross-owner access, malicious identifiers and generic provider error redaction.
- Scanned strict SQLite checkpoint bytes and Artifact Store metadata for credential markers; runtime-only connection/large-state markers do not persist.
- Evaluators constrain invariants rather than one natural-language answer or one internal action ordering.

Verification:

```text
LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest tests/integration/test_analysis_agent_trajectory.py tests/integration/test_analysis_agent_security.py tests/e2e -q -p no:cacheprovider
# 17 passed, 117 subtests passed

LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest tests/integration/test_analysis_agent_trajectory.py tests/integration/test_analysis_agent_security.py tests/integration/test_analysis_agent_runtime.py tests/integration/test_analysis_agent_resume.py tests/e2e -q -p no:cacheprovider
# 28 passed, 119 subtests passed
```

## Next exact actions (Task 13)

1. Inspect Studio/langgraph and CLI entrypoints against the native composition and replace wrapper/facade calls.
2. Update README and the reading guide to make Agent Runtime the only recommended execution path.
3. Regenerate OpenAPI/Apifox and frontend AgentResponse contracts using repository scripts.
4. Run Studio, CLI, contract freshness, packaging, frontend test and build gates.

## Completed Task 13 checkpoint

- Studio exports the native compiled 13-node Agent graph rather than a single wrapper node.
- The Studio graph binds a deterministic offline plan-mode composition. Its schema can be loaded and its example invoked directly without a model, datasource, Artifact Store or production checkpointer.
- Product graph construction still requires an explicit resolved context; the default binding is opt-in and isolated to the Studio adapter.
- CLI Plan, Preview and Execute forward all four datasource pins to the same native `analysis_runtime` used by the API, including compositions that expose no legacy `runtime` attribute.
- README and the project reading guide now describe the native Agent as the only default implementation and document checkpoints, resume/replay, version pins, evidence and validation commands.
- Regenerated `docs/apifox-openapi.json` and the frontend AgentResponse schema/fixture with repository scripts.
- Audited `pyproject.toml` package data and deliberately retained legacy Pack data-files until Task 14 proves their deletion through reachability and wheel inspection.

Verification:

```text
.venv/bin/python -m pytest tests/test_studio_adapter.py tests/test_main_cli.py tests/test_openapi_contract.py tests/test_frontend_agent_response_schema.py tests/contract/test_src_packaging.py -q -p no:cacheprovider
# 18 passed, 3 subtests passed
npm --prefix frontend test
# 16 files, 63 tests passed
npm --prefix frontend run build
# passed (1597 modules transformed)
```

## Next exact actions (Task 14)

1. Add `scripts/audit_repository_reachability.py` and validate it against source/test/script/frontend/package roots.
2. Record explicit reachability, exact-reference and wheel evidence in the removal manifest before changing any candidate to `approved-to-delete`.
3. Remove only proven caches and legacy product paths; retain possible user/secret state without reading it.
4. Run focused tests after every deletion batch, then the full backend/frontend/wheel/temporary-install gates.

## Completed Task 14 checkpoint

- Added a deterministic repository reachability auditor covering Python static imports, declarative lazy exports, package initialization, dynamic imports, product/script/test roots, subprocess commands, literal paths, frontend imports/scripts and Python package metadata.
- Final report (`/tmp/nl2sql-reachability-final.json`) has 92 product-reachable source files, no source orphan, and exactly one test/script-only source: retained `relationships/compat.py`, used by the explicit v1 relationship migration command.
- Extracted reusable dataset logical-plan and `PreparedQuery` contracts, model-client protocol, native composition roots and dataset-only Tool authority before deletion.
- Removed the API orchestration facade, fixed execution graph, Pack runtime/skills/providers, Commerce/OList Pack assets/generated files/scripts, their dedicated tests/fixtures, and duplicate `PackRuntimeVersionPins` response schema.
- Migrated connector tests to a current-contract `PreparedQuery` fixture and moved the native trajectory model double out of the deleted facade test.
- Removed the Memory-owned execution-checkpoint table/contracts/provider methods; LangGraph checkpointers remain the only resumability store while long-term Memory retains messages, summaries, proposals and approved records.
- Removed Pack-only package data and dependencies (`PyYAML`, `psycopg2-binary`, `jsonschema`). Fresh wheel inspection found 99 entries, a valid `RECORD`, every required native module, and zero retired-path matches.
- Cleaned only manifest-approved rebuildable caches/build outputs. Secret/user-state paths (`.env.codex-backup-*`, `.kaggle`, `.impeccable`, `.pnpm-store`, `var/data-agent`) were retained without inspecting secret contents.
- Historical plans/specs now carry explicit retained/superseded markers, and the reading guide is the single current architecture entrypoint.

Verification:

```text
.venv/bin/python -m pytest -p no:cacheprovider -q
# 337 passed, 70 subtests passed, 1 pre-existing Starlette deprecation warning
npm --prefix frontend test
# 16 files, 63 tests passed
npm --prefix frontend run build
# passed (1597 modules transformed)
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
# data_agent-0.1.0-py3-none-any.whl, sha256 9ffaf036d88c2e9eed70934f1cabb1773d3675350a6a7583755968e11132ec50
.venv/bin/python -m pytest tests/test_installed_cli.py -q -p no:cacheprovider
# 2 passed
```

## Next exact actions (Task 15)

1. Run the final mapped smoke suite for API auth/health, all datasource forms, all Agent modes, trajectory/replan, pause/restart/resume/cancel/replay and cross-owner denial.
2. Re-run backend/frontend/build/wheel gates after final dead-code and documentation edits.
3. Complete manifest/removal report, clean generated build outputs, then decide Task 16 merge readiness without touching protected local secrets or user-owned state.

## Completed Task 15 checkpoint

- Release smoke covered API health/login, datasource upload/catalog/binding, Agent question, CSV/XLSX/SQLite/PostgreSQL, Plan/Preview/Execute, multi-step/replan, pause/restart/resume, cancellation/replay, cross-owner denial and persistent-state redaction.
- The final response assertions cover plan revision/steps, ArtifactRefs, EvidenceRefs, immutable dataset/binding/schema/graph/model pins, bounded rows/chart and evidence-grounded answers.
- OpenAPI/Apifox and the browser-packaged AgentResponse schema/fixture are freshly generated and contain only dataset runtime pins.
- The final reachability report has no source orphan; the only non-product source is the intentionally retained relationship v1 migration compatibility module.
- Protected relationship editor/state changes remain present. Only the isolated Agent CSS block was added to the protected stylesheet; protected secret/state/package-manager directories were not read or deleted.
- A live DeepSeek request was not needed for the release gate: deterministic model doubles exercise exact planner/evaluator/synthesizer trajectories without external network variability or credential access.

Final source-branch verification:

```text
LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest -p no:cacheprovider -q
# 337 passed, 70 subtests passed, 1 pre-existing Starlette deprecation warning
npm --prefix frontend test
# 16 files, 63 tests passed
npm --prefix frontend run build
# passed (1597 modules transformed)
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
# sha256 3b99494921ed160824bf1c8dc80fb5e7b77b8f53beb357b10abd4f7e82dfa0a5
.venv/bin/python -m pytest tests/test_installed_cli.py -q -p no:cacheprovider
# 2 passed; wheel has 99 entries and zero retired-path matches
LANGGRAPH_STRICT_MSGPACK=true .venv/bin/python -m pytest <release-smoke-files> -q -p no:cacheprovider
# 90 passed, 42 subtests passed
```

## Next exact actions (Task 16)

1. Remove final verification outputs (`dist`, `frontend/dist`, build metadata and caches) and confirm the source-branch diff contains no secrets/user data.
2. Review the protected pre-existing changes as product changes, then create auditable source-branch commits without including `.env*`, user state or caches.
3. Re-read branch/main/origin ancestry, merge with an explicit merge commit only if the plan's clean-tree preconditions can be satisfied, and rerun the gates on local `main`.

## Completed Task 16 checkpoint

- Source branch: `Agent`; source commit: `4f7d11461d1c0d010a37131e71d00ad0cf63f6a2`.
- Pre-merge local `main` and `origin/main`: `64fc763de13de549fb4765f9ba41c6a46b11d981`; the branches were identical and `main` was an ancestor of the source branch.
- The primary worktree's four protected pre-existing untracked paths were left untouched. The merge and post-merge gates ran in a new isolated temporary `main` worktree, so no secret, cache, user state, or unconfirmed file was hidden, deleted, stashed, or committed.
- Local `main` merge commit: `cee85366634785fc8910a3d8b8354dad6bd79bcf` (`merge: migrate default runtime to data analysis agent`). The source commit is an ancestor of local `main`; the source branch was retained and nothing was pushed.
- Post-merge backend verification: `337 passed, 70 subtests passed`; the only warning is the pre-existing Starlette/httpx deprecation warning.
- Post-merge frontend verification: `16` files / `63` tests passed; Vite production build passed with `1597` modules transformed.
- Post-merge release smoke: `90 passed, 42 subtests passed`, covering API/auth/datasources, all Agent modes, multi-step/replan, pause/restart/resume, cancel/replay, and security boundaries.
- Post-merge wheel: SHA-256 `9a360b0f276048f4823247f783cb5858be9b97b9a3fa403e6661111d78d032b2`, `99` files, valid `RECORD`, and zero retired-path matches. An isolated wheel installation loaded the CLI, API, and native Agent graph from the installed wheel.
- All manifest-approved build/test outputs in the temporary worktree were removed; local `main` finished with a clean worktree.
- Rollback: do not reset or force-push. If the merge is later shared, use `git revert -m 1 cee85366634785fc8910a3d8b8354dad6bd79bcf`; if it remains local, report first and obtain confirmation before choosing any rollback operation.

## Important implementation decisions already frozen by source documents

- The model never submits arbitrary SQL or code; deterministic compiler/relationship routing/fan-out guard remain mandatory.
- All tool calls use Registry/Invoker, server-injected authority, budgets, grants, audit and redaction.
- Checkpoint state contains only JSON-safe small values and ArtifactRefs, never credentials, clients, connectors, paths, prompts, chain-of-thought or large result payloads.
- Default implementation must become a native dynamic LangGraph loop with durable pause/resume; no long-term dual workflow/agent primary path.
- Cleanup is gated by `docs/repository-removal-manifest.md`; no broad deletion commands.
