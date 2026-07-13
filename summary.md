# Data Agent v1 Summary

The product is now centered on one public `DataAgentRuntime`. CLI, FastAPI,
frontend, and Studio adapters submit an `AgentRequest` and consume typed
streaming events whose terminal carries an `AgentResponse`.

The six runtime layers are Runtime, Skill System, one bounded Execution Graph,
Tool Registry/Connector, Memory, and Enterprise Data Binding. The reusable
Commerce pack owns canonical entities, metrics, vocabulary, policies, and 48
eval questions. The OList enterprise pack owns PostgreSQL relations, fields,
join paths, allowlists, and seller ownership policy. New enterprises add a
binding rather than changing Commerce skills.

All three modes use the same graph:

- `plan`: validate the logical plan and compile governed SQL;
- `preview`: add credentials, EXPLAIN, and bounded preview;
- `execute`: run a bounded read-only query and render verified evidence.

Configuration is compiled with `data-agent compile-packs`, checked with
`data-agent validate-config`, and accompanied by a deterministic semantic
index from `data-agent rebuild-index`. The canonical generated bundle is
`generated/bundles/olist-local.json`.

CI uses an offline deterministic ModelClient, in-memory Memory, and a scoped
Connector fixture. The same Runtime executes all 48 canonical cases and checks
logical plan, binding relations, SQL AST, policy identity, seller/admin scope,
rows, answer evidence, trace, and version pins. Backend, frontend, production
build, wheel contents, and isolated-install smoke tests form the remaining
release gate.
