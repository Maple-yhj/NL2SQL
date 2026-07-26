# Data Agent

Data Agent is a governed, configuration-driven analytics runtime. The first
release combines the reusable `commerce` domain with the `olist` enterprise
binding. Enterprise differences live in database/table bindings; Commerce
metrics and analysis behavior remain enterprise-neutral.

## Architecture

All product adapters (CLI, FastAPI, frontend, and LangGraph Studio) call the
same public `data_agent.runtime.DataAgentRuntime`. The implementation is split
into six layers:

1. **Data Agent Runtime** — request lifecycle, identity, budgets, terminal
   events, version pins, and safe errors.
2. **Skill System** — Commerce vocabulary, typed `LogicalQueryPlan`, and plan
   validation; it contains no OList table or column names.
3. **Execution Graph** — one bounded graph for context, planning, binding,
   compile, execute, evidence validation, and answer rendering.
4. **Tool Registry** — six governed tools with typed contracts, policy checks,
   trace, and the PostgreSQL connector.
5. **Memory** — conversation history, approved recall, checkpoints, and
   pending proposals; memory cannot override packs or policy.
6. **Enterprise Data Binding** — OList sources, nine physical relations,
   field/relationship mapping, relation allowlist, and seller ownership rules.

The resolved deployment is compiled from:

- `packs/domains/commerce/` — entities, metrics, vocabulary, policies, and the
  canonical 48 eval cases;
- `packs/enterprises/olist/` — PostgreSQL relation/field binding and access
  policy;
- `packs/deployments/olist-local.yaml` — selects both packs and runtime limits;
- `schema_catalog.json` — attested physical schema metadata.

## Setup

Python 3.12 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Set `DATABASE_URL`, one model provider/key, and `JWT_SECRET_KEY`. The default
runtime uses the same PostgreSQL database for OList data, Data Agent memory,
and auth unless `AUTH_DATABASE_URL` is explicitly set.

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
psql $env:DATABASE_URL -f db/data_agent_memory.sql
psql $env:DATABASE_URL -f db/auth.sql
python scripts/import_olist_dataset.py --zip-path D:\data\olist.zip --reset
```

Auth seed commands require credentials supplied through their documented
arguments/environment; this repository does not publish a default password.

## CLI and three modes

```powershell
data-agent validate-config
data-agent ask "Show monthly GMV for 2018" --mode plan --role admin
data-agent ask "Show monthly GMV for 2018" --mode preview --role admin
data-agent ask "Show monthly GMV for 2018" --mode execute --role admin --include-trace
```

- `plan` returns the governed logical plan and compiled SQL without database
  credentials.
- `preview` runs cost checks and a bounded preview.
- `execute` runs the bounded read-only query and renders verified evidence.

Seller principals receive a parameterized `seller_id` ownership predicate;
configured admin roles receive policy bypass. Explicit questions such as “my
sales” remain typed `tenant_context` filters, distinct from policy scope.

## HTTP contract

Start the API with `uvicorn api.app:app --reload`. A question request uses the
new product contract only:

```json
{
  "question": "Show monthly GMV for 2018",
  "enterprise_id": "olist",
  "domain_id": "commerce",
  "mode": "execute",
  "requested_output": "answer",
  "include_trace": true
}
```

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

## External verified deployments

The default package still starts with the included OList bundle. A different
Commerce enterprise deployment can set `DATA_AGENT_BUNDLE_PATHS_FILE` to a
strict JSON descriptor with these keys:

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

Generated outputs are `generated/bundles/olist-local.json`,
`generated/semantic/commerce.json`, `docs/apifox-openapi.json`, and the
frontend files under `frontend/src/generated/`. Freshness tests fail if a
source pack or public schema changes without regeneration.

## Tests and builds

```powershell
python -m pytest -p no:cacheprovider
python -m pytest tests/e2e/test_olist_golden_runtime.py -q -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

The offline golden gate executes all 48 Commerce/OList cases through one
`DefaultDataAgentRuntime`, using a deterministic ModelClient, Memory, and
Connector. Each case checks logical and physical plans, policy, SQL, scoped
fixture results, answer evidence, trace, and version pins. Live model/database
checks are separate release checks and are not required in CI.
