# Data Agent

Data Agent is a governed analytics application driven entirely by data that
the user uploads or registers. The default API, web application, CLI, and
Studio runtime contain no preloaded business dataset and never fall back to
OList.

Users can upload CSV, XLSX, or SQLite files, or register a PostgreSQL source.
After catalog discovery they confirm a semantic binding, activate it, and only
then can start an analysis.

## Architecture

All product adapters use the native `DataAnalysisAgentRuntime` and compiled
LangGraph. The default product is split into four layers:

1. **Analysis Agent** — a bounded plan → guard → tool → observe → evaluate →
   replan loop with typed pause/resume and evidence-grounded synthesis.
2. **Datasource control plane** — immutable file snapshots, PostgreSQL
   registrations, catalog versions, semantic bindings, and conversation pins.
3. **Governed tools** — a dataset-specific Registry/Invoker backed by
   deterministic SQLGlot compilation, read-only connectors, Artifact Store and
   Evidence refs. Models never submit SQL or code.
4. **State authorities** — LangGraph checkpoints for an active run,
   conversation history for final turns, approved long-term Memory, and typed
   event replay for clients.

## Setup

Python 3.12 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Set `AUTH_DATABASE_URL`, one model provider/key, and `JWT_SECRET_KEY`.
`AUTH_DATABASE_URL` stores authentication only; uploaded data and control-plane
metadata live under `DATA_AGENT_STATE_DIR`.

The planner is provider-neutral. Set `LLM_PROVIDER` and an explicit
`DEFAULT_MODEL_NAME`, then configure the matching credential:

| Model family | `LLM_PROVIDER` | Credential | Optional endpoint override |
| --- | --- | --- | --- |
| GPT / OpenAI | `openai` or `gpt` | `OPENAI_API_KEY` | `OPENAI_BASE_URL` |
| Qwen | `qwen` | `DASHSCOPE_API_KEY` | `QWEN_BASE_URL` |
| Gemini | `google` or `gemini` | `GEMINI_API_KEY` | `GEMINI_BASE_URL` |
| Claude | `anthropic` or `claude` | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` |
| GLM | `glm` or `zhipu` | `ZAI_API_KEY` | `GLM_BASE_URL` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` |
| Other compatible service | `openai-compatible` | `LLM_API_KEY` | `LLM_BASE_URL` (required) |

Provider-specific adapters normalize system/user messages, output token
limits, and text-block responses behind the same runtime protocol. Qwen, GLM,
DeepSeek, and custom services use their OpenAI-compatible Chat Completions
endpoints; Gemini and Claude use native adapters. For Qwen production
deployments, set the workspace-specific `QWEN_BASE_URL` issued for the selected
Alibaba Cloud region.

```powershell
psql $env:AUTH_DATABASE_URL -f db/auth.sql
uvicorn api.app:app --reload
```

Auth seed commands require credentials supplied through their documented
arguments/environment; this repository does not publish a default password.

## Analysis modes

The Agent supports `plan`, `preview`, and `execute`. All modes require one
active semantic binding. Plan mode cannot execute a query; Preview allows only
bounded previews and marks conclusions accordingly; Execute allows governed
read-only execution within per-run tool/query/row/time budgets.

## HTTP contract

Question requests must include the four immutable pins from an active binding:

```json
{
  "question": "Show monthly revenue",
  "enterprise_id": "user-dataset",
  "domain_id": "dataset.orders",
  "source_id": "orders",
  "source_version": 1,
  "binding_id": "orders-binding-1",
  "binding_version": 1,
  "mode": "execute",
  "requested_output": "answer",
  "include_trace": true
}
```

A request without these pins receives a typed validation failure asking the
user to upload and activate a dataset. It does not query a bundled fallback.

The terminal `AgentResponse` contains the analysis plan and completed steps,
artifact/evidence summaries, logical plan, compiled SQL, bounded rows/chart,
answer/limitations, safe trace, pending memory proposals, and immutable version
pins.

For incremental delivery, use `POST /api/nl2sql/stream` or
`POST /api/conversations/{id}/messages/stream`. Both return typed SSE events.
Active or waiting runs can be cancelled through
`POST /api/runs/{run_id}/cancel`. A `run_waiting` event carries an interrupt ID;
continue it through `POST /api/runs/{run_id}/resume` or `/resume/stream`.
`GET /api/runs/{run_id}/events` replays the complete monotonic event sequence.
Run control, checkpoints, artifacts and replay are always scoped to the
authenticated tenant and user.

Local durable state defaults to `DATA_AGENT_STATE_DIR`: SQLite LangGraph
checkpoints, run events, conversation records and Artifact Store metadata have
separate files and lifecycles. Production may configure the optional Postgres
checkpointer; checkpoint serialization has no pickle fallback.

## CLI and LangGraph Studio

CLI Plan, Preview and Execute call the same Agent Runtime as the API. Supply the
four active datasource pins:

```powershell
data-agent ask "Show monthly revenue" --mode execute `
  --domain-id dataset.orders --source-id orders --source-version 1 `
  --binding-id orders-binding-1 --binding-version 1
```

`langgraph.json` exports `src/data_agent/adapters/studio.py:graph`, the real
compiled Agent graph. Importing it binds a deterministic offline plan-mode
development context: it exposes the complete topology and a runnable example
without opening a model client, checkpoint database, Artifact Store or
production datasource. Product API/CLI execution still injects the resolved
per-request `AnalysisGraphContext` into the same graph builder.

Pending memory changes are listed with `GET /api/memory/proposals` and decided
through `POST /api/memory/proposals/{proposal_id}/decision`. User-owned memory
is visible only to its owner; enterprise and episodic memory require a memory
administrator.

## User-selected datasources

The web datasource panel accepts CSV, XLSX, and SQLite uploads, discovers their
catalog, and requires an explicit semantic-field confirmation before querying.
Uploads become immutable read-only snapshots under `DATA_AGENT_STATE_DIR`;
registry metadata, binding versions, and conversation pins are stored in the
separate control-plane SQLite database.

PostgreSQL registration accepts a `credential_ref`, never a password. A
reference such as `secret://team/warehouse` is resolved at runtime from
`DATA_SOURCE_SECRET_TEAM_WAREHOUSE`. The first catalog request opens the
deployment-side secret, snapshots the accessible schema, and registers the same
governed read-only connector used by the query path.

Datasource-backed requests pin all four authority fields together:

```json
{
  "source_id": "orders",
  "source_version": 1,
  "binding_id": "orders-binding-1",
  "binding_version": 1
}
```

The model sees logical field references only. Physical identifiers are added
later by the deterministic compiler, and stale or cross-conversation pins are
rejected before planning.

## Relationship graphs for uploaded data sources

Every published datasource now receives a revisioned relationship-graph draft.
The browser lets an analyst add business-role nodes (including self-join roles),
define field relationships, validate them, preview the deterministic route, and
explicitly activate an immutable v2 binding. Recommendation prompts contain
only catalog IDs and metadata; they never contain connection credentials or raw
data values. Query compilation resolves only the required graph route, uses
role-specific SQL aliases, supports composite equality predicates, and rejects
ambiguous paths or unsafe aggregate fan-out.

The legacy v1 tree bindings remain readable. The migration helper creates a v2
draft for review without activating it:

```powershell
python scripts/migrate_v1_binding_to_graph.py binding.json catalog.json --preview
python scripts/migrate_v1_binding_to_graph.py binding.json catalog.json --execute --output graph-draft.json
```

## Contract generation

```powershell
python scripts/export_apifox_openapi.py
python scripts/export_frontend_agent_response_schema.py
```

Public contract outputs are `docs/apifox-openapi.json` and the frontend files
under `frontend/src/generated/`. Freshness tests fail when a public schema
changes without regeneration.

## Tests and builds

```powershell
python -m pytest -p no:cacheprovider
python -m pytest tests/integration/test_analysis_agent_trajectory.py tests/integration/test_analysis_agent_security.py -q -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

Live model/database checks are separate release checks and are not required in
CI.
