# Data Agent

Data Agent is a governed analytics application driven entirely by data that
the user uploads or registers. The default API, web application, CLI, and
Studio runtime contain no preloaded business dataset and never fall back to
OList.

Users can upload CSV, XLSX, or SQLite files, or register a PostgreSQL source.
After catalog discovery they confirm a semantic binding, activate it, and only
then can start an analysis.

## Architecture

All product adapters call the same public `DataAgentRuntime`. The default
product is split into four layers:

1. **Upload runtime** — identity-scoped conversations and a fail-closed
   boundary when no datasource has been selected.
2. **Datasource control plane** — immutable file snapshots, PostgreSQL
   registrations, catalog versions, semantic bindings, and conversation pins.
3. **Dataset query service** — structured Planner output, deterministic Query
   IR compilation, read-only execution, evidence validation, and safe charts.
4. **Run control** — typed SSE, cancellation, replay, and safe terminal errors.

The Commerce/OList pack runtime remains in the repository only as an explicit
compatibility path and deterministic regression fixture. It is not loaded by
normal application startup.

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

The user-selected datasource path supports `plan`, `preview`, and `execute`.
All three require one active semantic binding. `plan` returns a structured plan
and compiled read-only SQL; `preview` and `execute` additionally use bounded
query execution.

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

The terminal `AgentResponse` contains the logical plan, compiled SQL, bounded
rows, answer, safe trace, pending memory proposals, and immutable version pins.

For incremental delivery, use `POST /api/nl2sql/stream` or
`POST /api/conversations/{id}/messages/stream`. Both return typed SSE events.
Active runs can be cancelled through `POST /api/runs/{run_id}/cancel`, and
persisted events can be resumed with `GET /api/runs/{run_id}/events`. Run
control and replay are always scoped to the authenticated tenant and user.

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

## Optional governed-pack runtime

The legacy Commerce pack runtime is opt-in for compatibility and offline
regression testing. Call `build_runtime`/`build_olist_runtime` explicitly; the
default application factory never calls them. A custom verified deployment can
set `DATA_AGENT_BUNDLE_PATHS_FILE` to a strict JSON descriptor:

```json
{
  "domain_root": "domain",
  "enterprise_root": "enterprise",
  "deployment_profile": "deployment.yaml",
  "pack_lock": "enterprise/pack.lock",
  "schema_catalog": "schema-catalog.json",
  "bundle_manifest": "bundle.json"
}
```

Relative paths are resolved from the descriptor directory. All six inputs must
exist and still pass the normal bundle digest, source attestation, profile, and
schema checks before a model client or database pool is created.

## Pack and contract generation

```powershell
data-agent compile-packs
data-agent rebuild-index
data-agent validate-config
python scripts/export_apifox_openapi.py
python scripts/export_frontend_agent_response_schema.py
```

The first two commands maintain the optional OList compatibility fixture.
Public contract outputs are `docs/apifox-openapi.json` and the frontend files
under `frontend/src/generated/`. Freshness tests fail when a public schema
changes without regeneration.

## Tests and builds

```powershell
python -m pytest -p no:cacheprovider
python -m pytest tests/e2e/test_olist_golden_runtime.py -q -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

The offline OList golden gate is an explicit compatibility test and never
imports or deploys OList during normal startup. Live model/database checks are
separate release checks and are not required in CI.
