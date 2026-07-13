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
