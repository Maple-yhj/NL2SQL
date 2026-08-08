# Project Reading Guide

This is the only recommended entry point for the current architecture.

## Start with the native Agent Runtime

Read these files in order:

1. `src/data_agent/analysis_agent/runtime.py` — public run, resume, cancel and checkpoint-owner boundary.
2. `src/data_agent/analysis_agent/graph.py` and `nodes.py` — the native LangGraph plan → guard → tool → observe → evaluate → replan loop.
3. `src/data_agent/analysis_agent/models.py` and `state.py` — immutable authority, plan, artifact/evidence and bounded checkpoint state.
4. `src/data_agent/analysis_agent/composition.py` — active datasource resolution, tool context, Artifact Store, version pins and final response construction.
5. `src/data_agent/runtime/models.py` and `events.py` — public request/response and typed SSE contracts.

The production adapters are `src/api/`, `src/data_agent/cli.py`, and
`src/data_agent/adapters/studio.py`. API and CLI call the same
`DataAnalysisAgentRuntime`. Studio exports the same compiled graph directly and
binds a deterministic offline plan-mode context, so its example is directly
runnable without connecting to a model or production datasource at import time.

## Follow the current layers

### 1. Datasource authority

`src/api/datasource_service.py` and `src/data_agent/datasources/` own immutable
file/PostgreSQL snapshots, catalogs, semantic bindings, relationship graphs and
conversation pins. A run must supply the complete source/binding version tuple.

### 2. Agent orchestration

`analysis_agent/graph.py` is the only default orchestration path. Planner,
Evaluator and Synthesizer receive bounded untrusted data and strict schemas.
Every model action passes `guard_decision`; only the guarded path reaches a
tool. The graph supports multiple tools, replan, typed interrupt and durable
resume.

### 3. Governed tools and deterministic queries

`src/data_agent/tools/providers/dataset/` defines the independent 12-tool
dataset registry. `ToolInvoker` enforces mode, authority, budget, grants,
credentials and redaction. `src/data_agent/dataset_query/` owns structured
query planning, SQLGlot compilation, read-only execution and safe result/chart
rendering. The model never supplies SQL or code.

### 4. Artifact and evidence authority

`analysis_agent/artifacts.py` stores result payloads outside checkpoints and
returns owner/run-scoped references. Evidence binds claims to artifacts and to
the exact source/binding/schema pins. Final answers can cite only validated
evidence from the current run.

### 5. Checkpoints, conversations and Memory

- LangGraph Checkpointer: short-lived current-run state for pause/restart/resume.
- `SQLiteConversationRepository`: user-visible conversation history and one
  idempotent final turn per run.
- `data_agent/memory/`: separately approved long-term Memory. The Agent may
  propose; it cannot approve or commit its own proposal.

These stores are intentionally separate. Connectors, credentials, clients,
large rows, prompts and private reasoning never enter checkpoint state.

## Trace one request

1. API/CLI validates `AgentRequest` and authenticated `PrincipalContext`.
2. Resolver loads the exact active datasource/binding and creates immutable authority pins.
3. Context loader adds bounded conversation history and committed Memory.
4. Planner creates or revises an `AnalysisPlan` using the allowed registry view.
5. Guard validates mode, budget and strict tool input before `ToolInvoker`.
6. Tools create owner-scoped artifacts and evidence; Evaluator decides continue, replan, clarify or finish.
7. Synthesizer creates an evidence-grounded answer and the graph validates it.
8. `persist_turn` atomically records one complete final turn; waiting runs record only checkpoint/events.
9. Runtime emits exactly one `run_completed`, `run_failed`, or `run_waiting` closing event.

## Tests to read

- `tests/unit/analysis_agent/`: strict models, graph, guards and model components.
- `tests/unit/tools/dataset/`: dataset registry/provider authority.
- `tests/integration/test_analysis_agent_runtime.py` and `_resume.py`: runtime/checkpointer behavior.
- `tests/integration/test_analysis_agent_trajectory.py`: multi-step result invariants.
- `tests/integration/test_analysis_agent_security.py`: adversarial gates.
- `tests/test_api_resume.py` and `frontend/src/agent/`: API and browser waiting/resume/replay behavior.

## Legacy status

The fixed Commerce/OList Pack runtime and its orchestration facade were retired
after the Task 14 reachability audit. Historical plans remain decision records,
not implementation entrypoints. New work should use the native Analysis Agent,
the dataset query contracts/providers, and the datasource control plane listed
above.
