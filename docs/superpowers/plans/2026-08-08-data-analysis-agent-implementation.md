# Data Analysis Agent 实施与仓库清理计划

> **交接给下一实现会话：** 严格按 Task 顺序执行并维护复选框。当前工作区存在用户未提交改动，尤其是 `frontend/src/relationships/` 和 `frontend/src/styles.css`。这些改动属于用户，开始每个 Task 前先检查重叠 diff，不得 reset、checkout、覆盖或顺手格式化无关文件。

**设计依据：** `docs/superpowers/specs/2026-08-08-data-analysis-agent-design.md`

**目标：** 将默认用户数据源 NL2SQL Workflow 迁移为原生 LangGraph、可持久化、可暂停恢复、可多步工具调用和有界重规划的 Data Analysis Agent；完成切换和黄金能力迁移后，删除当前及目标架构不再使用的代码、目录、配置、生成物、依赖和测试。

**技术栈：** Python 3.12、Pydantic v2、FastAPI、LangGraph 1.2+、LangChain provider adapters、SQLGlot、DuckDB、SQLite、PostgreSQL、React 19、TypeScript、Vite、Vitest、pytest。

**迁移策略：** 逐层抽取、测试内双跑、单路切换、最后清理；不长期保留 workflow/agent 双主链路。

---

## 0. 不可变实施规则

- 新 Agent 不能直接执行模型生成 SQL；所有 SQL 必须经过现有确定性 Compiler、关系路由和 Fan-out Guard。
- 新 Agent 工具不能直接读取环境变量或创建数据库连接；凭据只由 composition/broker 注入。
- source/binding 四项 pins 在一个 run 内不可变，resume 时重新验证。
- Agent state/checkpoint 不保存凭据、连接字符串、文件句柄、大型结果集、Prompt 或 chain-of-thought。
- 所有模型输出先经过严格 Pydantic 校验，再进入 routing 或 ToolInvoker。
- 所有工具调用都经过 Registry/Invoker、Decision Guard、预算、超时、AccessGrant、审计和脱敏。
- 默认只读；不增加任意 Python/Shell/JavaScript 执行。
- 先补测试，再改实现；一个 Task 完成后运行该 Task 的 focused tests。
- 公共模型、OpenAPI 或前端类型变化必须同步生成并通过 freshness tests。
- 删除动作必须基于 removal manifest；不得使用 `git clean -fdx`、仓库根目录递归删除或宽泛 glob。
- 对 ignored/generated 文件只能删除 manifest 中验证过的显式路径；删除后报告内容、是否可重建和恢复命令。
- 不修改或读取 `.env`、备份 credential 文件和 `.kaggle` 内的秘密内容；若 manifest 确认应删除，只按路径删除，不输出内容。

## 1. 当前基线与最终完成定义

### 1.1 当前主链路

```text
FastAPI routes
  → DataSourceQueryService
  → DatasetLogicalPlanner
  → DatasetQueryCompiler
  → Connector preview/execute
  → AgentResponse
```

### 1.2 最终主链路

```text
FastAPI routes
  → DataAnalysisAgentRuntime
  → LangGraph StateGraph
  → plan / guard / tool / observe / evaluate / replan loop
  → synthesize / validate / persist
  → AgentResponse + typed SSE + checkpoint + evidence
```

### 1.3 完成定义

- [x] 默认 `/api/nl2sql` 和 conversation message 路由不再直接调用 `DataSourceQueryService.run/stream`。
- [x] 默认 composition 构建原生 LangGraph Agent 和 durable checkpointer。
- [x] Agent 至少可以在一个 run 中执行多个不同工具并根据 observation 改变下一步。
- [x] clarification/approval 可以 pause、服务重启、resume 并完成。
- [x] Plan/Preview/Execute 权限语义正确。
- [x] 每个关键数值结论都有 EvidenceRef。
- [x] 当前单查询结果级回归测试通过。
- [x] 多步、重规划、暂停恢复、安全和预算测试通过。
- [x] 前端能展示步骤、工具状态、等待输入、恢复和证据。
- [x] 旧默认 Workflow、仅旧 Pack/OList 使用的代码和仓库垃圾完成审计及删除。
- [x] 全量测试、前端构建、wheel 构建和安装测试通过。
- [x] README、reading guide、OpenAPI、Apifox、前端生成契约与新架构一致。
- [x] 所有实现变更在当前开发分支形成可审计提交，并在最终验证后合并到本地 `main` 分支。

## 2. 目标文件结构

### 2.1 新增后端模块

```text
src/data_agent/analysis_agent/
├── __init__.py
├── models.py
├── state.py
├── prompts.py
├── planner.py
├── evaluator.py
├── synthesizer.py
├── guard.py
├── routing.py
├── nodes.py
├── graph.py
├── runtime.py
├── artifacts.py
├── checkpoints.py
└── composition.py

src/data_agent/tools/providers/dataset/
├── __init__.py
├── catalog.py
├── semantic.py
├── relationship.py
├── query.py
├── profile.py
├── compute.py
├── chart.py
├── evidence.py
└── registry.py
```

### 2.2 新增测试模块

```text
tests/unit/analysis_agent/
├── test_models.py
├── test_state.py
├── test_planner.py
├── test_evaluator.py
├── test_guard.py
├── test_routing.py
├── test_artifacts.py
├── test_nodes.py
└── test_graph.py

tests/unit/tools/dataset/
├── test_catalog_provider.py
├── test_query_providers.py
├── test_compute_provider.py
├── test_chart_provider.py
└── test_evidence_provider.py

tests/integration/
├── test_analysis_agent_runtime.py
├── test_analysis_agent_resume.py
├── test_analysis_agent_security.py
└── test_analysis_agent_trajectory.py
```

实际实现可合并过小文件，但职责边界和测试覆盖不得丢失。

## 3. Task 0 — 冻结基线、保护用户改动并建立迁移记录

### 文件

- Create: `docs/data-analysis-agent-baseline.md`
- Create: `docs/repository-removal-manifest.md`
- Modify: `.gitignore`（仅在确认生成物模式后）

### 步骤

- [x] 运行 `git status --short` 和 `git diff --stat`，把已有修改摘要写入 baseline，不复制秘密内容。
- [x] 记录 Python、Node、npm 版本及当前依赖安装方式。
- [x] 运行当前 focused backend tests：

```bash
.venv/bin/python -m pytest \
  tests/test_api_nl2sql.py \
  tests/test_dataset_query_service.py \
  tests/test_run_streams.py \
  tests/integration/test_file_datasource.py \
  tests/integration/test_sqlite_connector.py \
  tests/integration/test_postgres_connector.py \
  -q -p no:cacheprovider
```

- [x] 运行当前前端测试和构建：

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

- [x] 运行当前完整后端测试并记录通过/失败/跳过数量：

```bash
.venv/bin/python -m pytest -p no:cacheprovider
```

- [x] 记录 wheel 构建基线：

```bash
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

- [x] 创建 removal manifest 初始表，字段至少包括：

```text
Path | Tracked | Category | Current references | Future replacement
Decision | Evidence commands | Delete task | Rebuild/restore method | Status
```

- [x] 将当前发现的候选项加入 manifest，但全部标为 `unverified`，不得在本 Task 删除。

### 初始候选分类

**本地可重建生成物/缓存：**

```text
**/__pycache__/
.pytest_cache/
build/
dist/
*.egg-info/
frontend/dist/
frontend/node_modules/
.pnpm-store/
generated/runtime-path-tests/
generated/runtime-source-tests/
var/data-agent/ 中明确属于本地测试的状态
docs/superpowers/.DS_Store
```

**旧根目录残留：** 当前 `api/`、`catalog/`、`core/`、`engine/`、`graph/`、`rag/` 若只包含缓存或已迁移旧代码，则列入候选；必须先用 `git ls-files`、`find` 和 import/reference 审计确认。

**敏感或工具临时目录：** `.env.codex-backup-*`、`.kaggle/`、`.impeccable/` 只记录路径与用途判断，不读取或输出秘密。只有确认不属于用户仍需资产后才在最终清理 Task 删除。

### 验收

- [x] baseline 包含准确命令与结果。
- [x] removal manifest 已建立且没有执行删除。
- [x] 用户现有修改没有被覆盖。

## 4. Task 1 — 建立 Agent 公共领域模型与状态契约

### 文件

- Create: `src/data_agent/analysis_agent/models.py`
- Create: `src/data_agent/analysis_agent/state.py`
- Create: `src/data_agent/analysis_agent/__init__.py`
- Create: `tests/unit/analysis_agent/test_models.py`
- Create: `tests/unit/analysis_agent/test_state.py`
- Modify: `src/data_agent/runtime/errors.py`

### 测试先行

- [x] 为 `DatasetAuthority` 编写四项 pins、tenant/user、mode 和 allowlist 校验测试。
- [x] 为 `AnalysisGoal`、`AnalysisPlan`、`AnalysisStep` 编写严格字段、唯一 ID、依赖无环和 revision 测试。
- [x] 为 `AgentAction` 编写禁止 authority 字段、工具名格式和 JSON 参数测试。
- [x] 为 `AgentObservation`、`AgentArtifactRef`、`EvidenceRef` 编写所有权和 digest 测试。
- [x] 为 `PlannerDecision`、`EvaluationDecision`、`AgentAnswerDraft` 编写 discriminator 与互斥字段测试。
- [x] 为 append reducers 编写顺序、去重、冲突和不可变测试。
- [x] 为 Agent status 转换编写有限状态机测试。

### 实现

- [x] 使用 Pydantic frozen/extra-forbid 模型；公共模型禁止 unchecked `model_construct`。
- [x] State 使用 `TypedDict`，append-only 字段使用 `Annotated` reducer。
- [x] 在模型层禁止保存 `secret`、`credential`、`dsn`、`raw_sql`、`file_path` 等模型可控字段。
- [x] 新增设计文档中的 Agent 错误码。
- [x] 加入稳定 digest helper；digest 只基于规范化 JSON。
- [x] 确保所有 checkpoint state 可被 LangGraph 默认 JSON serializer 处理，不启用 pickle fallback。

### 验证

```bash
.venv/bin/python -m pytest tests/unit/analysis_agent/test_models.py tests/unit/analysis_agent/test_state.py -q -p no:cacheprovider
```

### 验收

- [x] 非法模型输出不能进入 state。
- [x] State 不包含运行时资源或秘密。
- [x] reducers 在重复 resume/replay 时保持幂等。

## 5. Task 2 — 扩展公共响应、Version Pins 和类型化事件

### 文件

- Modify: `src/data_agent/runtime/models.py`
- Modify: `src/data_agent/runtime/events.py`
- Modify: `src/data_agent/runtime/errors.py`
- Modify: `src/api/schemas.py`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/agentResponseValidator.ts`
- Add/Modify: `tests/unit/runtime/test_typed_public_events.py`
- Add/Modify: `tests/test_frontend_agent_response_schema.py`
- Add/Modify: `tests/test_openapi_contract.py`
- Add: frontend event contract tests

### 实现顺序

- [x] 先扩展 backend contract tests，确保旧 terminal invariant 仍成立。
- [x] 新增 `DatasetRuntimeVersionPins` 与 `PackRuntimeVersionPins` discriminator union。
- [x] 给 `AgentResponse` 增加 plan/steps/artifacts/evidence/limitations，旧 convenience fields 保留。
- [x] 将 AgentEvent payload 改为严格 union；增加 context/plan/step/tool/observation/waiting/resumed/synthesizing 类型。
- [x] 定义 stream-closing 状态：completed、failed、waiting；waiting 不携带成功 `AgentResponse`，而携带 `AgentInputRequest`。
- [x] 更新 RunEventStore 允许持久化 `waiting`，并确保 sequence 单调且同序列 payload 幂等。
- [x] 更新前端 TypeScript union 和 runtime validator。
- [x] 暂不改变 API 路由行为；当前 workflow 继续只发旧事件子集。

### 验证

```bash
.venv/bin/python -m pytest \
  tests/unit/runtime/test_typed_public_events.py \
  tests/test_frontend_agent_response_schema.py \
  tests/test_openapi_contract.py \
  tests/test_run_streams.py \
  -q -p no:cacheprovider
npm --prefix frontend test
```

### 验收

- [x] 事件中无无类型长期 payload。
- [x] 旧 workflow 仍能通过现有 API 测试。
- [x] waiting 事件可回放且不被误判为失败/完成。

## 6. Task 3 — 实现租户隔离 Artifact Store

### 文件

- Create: `src/data_agent/analysis_agent/artifacts.py`
- Create: `tests/unit/analysis_agent/test_artifacts.py`
- Add: artifact cleanup/retention tests

### 测试先行

- [x] 写入/读取 JSON artifact 并校验 digest。
- [x] 同一 call/digest 重复写入返回同一 ArtifactRef。
- [x] 相同 artifact_id 不允许跨 tenant/user/run 读取。
- [x] 拒绝 path traversal、绝对路径和用户控制扩展名。
- [x] payload 被篡改时返回 integrity error。
- [x] 临时文件失败不会留下可见半成品。
- [x] retention 只删除已过期且不被保留的 artifact。
- [x] 大型 payload 不进入 checkpoint state。

### 实现

- [x] SQLite metadata 存放 ownership、kind、digest、schema、row count、sensitivity、retention。
- [x] payload 路径由服务端 hash/ID 生成。
- [x] 使用临时文件和原子 rename；不要使用 pickle。
- [x] 提供 `put_json/get_json/get_safe_preview/list_for_run/delete_expired`。
- [x] safe preview 限制行、列、字符和嵌套深度。
- [x] 增加敏感列 redaction hook，默认策略保守。

### 验证

```bash
.venv/bin/python -m pytest tests/unit/analysis_agent/test_artifacts.py -q -p no:cacheprovider
```

### 验收

- [x] checkpoint 只保存 ArtifactRef。
- [x] artifact 访问以 tenant/user/run 三重隔离。
- [x] 所有路径和 digest 测试通过。

## 7. Task 4 — 抽取当前 NL2SQL 确定性能力

### 目标

在不改变当前 API 行为的前提下，把 `src/api/dataset_query_service.py` 中的职责拆成可被 workflow 和 Agent 共用的服务。

### 文件

- Create: `src/data_agent/dataset_query/models.py`
- Create: `src/data_agent/dataset_query/planner.py`
- Create: `src/data_agent/dataset_query/compiler.py`
- Create: `src/data_agent/dataset_query/executor.py`
- Create: `src/data_agent/dataset_query/results.py`
- Create: `src/data_agent/dataset_query/__init__.py`
- Modify: `src/api/dataset_query_service.py`
- Move/adapt tests from `tests/test_dataset_query_service.py`

### 步骤

- [x] 为现有 `DatasetQueryPlan`、patch/follow-up、compiler 和 response 保持测试建立基线。
- [x] 迁移模型，不改变 JSON schema。
- [x] 抽取 `DatasetLogicalPlanner`；保持 provider-neutral `ModelClient`。
- [x] 抽取 `DatasetQueryCompiler`；继续使用 SQLGlot、GraphRouteResolver 和 FanoutGuard。
- [x] 抽取 `DatasetQueryExecutor`；只接受 PreparedQuery、authority/grant、connector 和 mode。
- [x] 抽取 result rows/chart/answer 纯函数。
- [x] 将原 `DataSourceQueryService` 改为薄 orchestration facade，旧 tests 必须仍通过。
- [x] 不在本 Task 引入 Agent 或改变 route。

### 验证

```bash
.venv/bin/python -m pytest \
  tests/test_dataset_query_service.py \
  tests/unit/relationships \
  tests/unit/tools/test_grain_alignment_lowering.py \
  tests/integration/test_file_datasource.py \
  tests/integration/test_sqlite_connector.py \
  tests/integration/test_postgres_connector.py \
  -q -p no:cacheprovider
```

### 验收

- [x] 当前单查询 API 输出不变。
- [x] Compiler/Executor 不再依赖 FastAPI。
- [x] Agent 后续可直接复用这些服务。

## 8. Task 5 — 泛化 Tool Authority 并建立 Dataset Agent Registry

### 文件

- Modify: `src/data_agent/tools/models.py`
- Modify: `src/data_agent/tools/invoker.py`
- Modify: `src/data_agent/tools/registry.py`
- Create: `src/data_agent/tools/providers/dataset/*`
- Create: `tests/unit/tools/dataset/*`
- Modify: existing Tool Registry/Invoker tests

### 关键决策

- 保留 Pack tools 的行为直到旧链路退役；通过 `AuthorityEnvelope` 支持 `pack` 与 `dataset`。
- 新 registry 与旧 registry 分开构建，避免工具名和 bundle 假设互相污染。
- 模型只看到允许工具的名称、用途和输入 schema 摘要，不看到 provider、credential 或内部 policy。

### 步骤

- [x] 新增 `PackAuthority`/`DatasetAuthority` discriminator。
- [x] ToolSpec 增加 authority kinds、allowed modes、artifact policy 和 credential requirement。
- [x] ToolInvocationContext 从强制 bundle 改为 authority envelope；保持 legacy adapter 测试。
- [x] Decision Guard 服务端注入 authority，拒绝 payload 中的 source/tenant/user/pins 覆盖。
- [x] 实现 `catalog.inspect`、`semantic.inspect`、`relationship.route`。
- [x] 实现 `query.compile`、`query.explain`、`query.preview`、`query.execute`，内部复用 Task 4 服务。
- [x] 实现 `data.profile` 和 `result.profile`。
- [x] 实现受限 `analysis.compute` DSL，禁止任意代码。
- [x] 实现 `chart.render` 与 `evidence.collect`。
- [x] 工具结果写 Artifact Store，返回小型结构化 metadata/preview。
- [x] ToolInvoker 记录 call_id、budget、latency、safe args digest、artifact/evidence refs。

### 安全测试

- [x] Plan 调用 credential tool 被拒绝。
- [x] Preview 调用 full execute 被拒绝。
- [x] artifact 不属于当前 run 被拒绝。
- [x] 模型试图修改 pins 被拒绝。
- [x] 任意 SQL/代码参数不在 schema 中，被 extra-forbid 拒绝。
- [x] 只读、行数、超时和 allowed relations 继续生效。

### 验证

```bash
.venv/bin/python -m pytest \
  tests/unit/tools \
  tests/unit/analysis_agent/test_artifacts.py \
  tests/integration/test_file_datasource.py \
  tests/integration/test_sqlite_connector.py \
  tests/integration/test_postgres_connector.py \
  -q -p no:cacheprovider
```

### 验收

- [x] 用户数据源工具不再绕过统一 Invoker。
- [x] Legacy pack tool tests 仍通过。
- [x] 所有工具输出可作为 AgentObservation 输入。

## 9. Task 6 — 实现 Planner、Evaluator、Synthesizer 与 Prompt 防护

### 文件

- Create: `src/data_agent/analysis_agent/prompts.py`
- Create: `src/data_agent/analysis_agent/planner.py`
- Create: `src/data_agent/analysis_agent/evaluator.py`
- Create: `src/data_agent/analysis_agent/synthesizer.py`
- Create: `tests/unit/analysis_agent/test_planner.py`
- Create: `tests/unit/analysis_agent/test_evaluator.py`
- Add: synthesis/grounding tests

### Planner

- [x] 输入只包含 goal、catalog/binding 摘要、当前 plan、safe observations、预算余额和允许工具 schema。
- [x] 输出严格 `PlannerDecision`。
- [x] 非法 JSON、未知工具、未知字段、重复 step ID 和循环依赖进入 bounded correction；超过次数失败。
- [x] follow-up 从会话摘要重建 goal，不复制上一个 run 临时 action。

### Evaluator

- [x] 先运行确定性 evidence/result checks。
- [x] 模型只能从 continue/replan/clarify/finish/fail 中选择。
- [x] 空结果、schema mismatch、contradiction 和 insufficient evidence 有明确处理。
- [x] 模型不能把 ToolError 改写为成功。

### Synthesizer

- [x] 输入只包含 validated evidence 和 bounded safe observations。
- [x] 每个数值 finding 必须引用 evidence_id。
- [x] Preview 模式自动附加“基于预览”限制。
- [x] 没有足够 evidence 时不得生成确定性结论。

### Prompt Injection 测试

- [x] 单元格文本包含系统指令、工具调用伪造 JSON、Markdown fence、SQL 和 prompt injection。
- [x] 列名/表名包含恶意指令文本。
- [x] Tool error 包含路径、DSN 或 provider 原始错误。
- [x] Prompt builder 始终把这些内容放在 data 区域并进行长度/字符限制。

### 验证

```bash
.venv/bin/python -m pytest \
  tests/unit/analysis_agent/test_planner.py \
  tests/unit/analysis_agent/test_evaluator.py \
  tests/unit/analysis_agent -q -p no:cacheprovider
```

### 验收

- [x] 三类模型调用均 provider-neutral。
- [x] 不保存或返回 chain-of-thought。
- [x] 未引用证据的数值回答被确定性 validator 拒绝。

## 10. Task 7 — 构建原生 LangGraph Agent

### 文件

- Create: `src/data_agent/analysis_agent/guard.py`
- Create: `src/data_agent/analysis_agent/routing.py`
- Create: `src/data_agent/analysis_agent/nodes.py`
- Create: `src/data_agent/analysis_agent/graph.py`
- Create: `tests/unit/analysis_agent/test_guard.py`
- Create: `tests/unit/analysis_agent/test_routing.py`
- Create: `tests/unit/analysis_agent/test_nodes.py`
- Create: `tests/unit/analysis_agent/test_graph.py`

### 图结构

```text
START
  → initialize_run
  → load_context
  → plan_or_replan
  → guard_decision
      → execute_tool
      → request_input
      → synthesize_answer
      → fail
  → observe_result
  → evaluate_progress
      → plan_or_replan
      → request_input
      → synthesize_answer
      → fail
  → validate_answer
  → persist_turn
  → finalize_run
  → END
```

### 步骤

- [x] 每个 node 只接收 state + injected runtime context。
- [x] 用条件 edge 和受限 route enum，不接受模型提供节点名。
- [x] 每个 node 进入前检查 deadline/cancellation。
- [x] model/tool/replan 预算在执行前原子消费。
- [x] request_input 使用 `interrupt()`，payload 只含 `AgentInputRequest`。
- [x] 工具调用以 stable call_id 实现 resume 幂等。
- [x] 将 LangGraph custom/updates stream 转换为公共 AgentEvent，而不是直接暴露内部 chunk。
- [x] graph version/digest 进入 DatasetRuntimeVersionPins。
- [x] 计算静态硬上限，LangGraph recursion_limit 大于 max steps 但仍为有限值。

### 测试场景

- [x] 一次 action 后完成。
- [x] 两个不同工具后完成。
- [x] 空 preview 后 replan。
- [x] tool failure 后 bounded correction。
- [x] model invalid decision 后 correction/fail。
- [x] clarification interrupt。
- [x] budget/deadline/cancel。
- [x] Plan/Preview/Execute route 差异。
- [x] exactly one terminal response。

### 验证

```bash
.venv/bin/python -m pytest tests/unit/analysis_agent -q -p no:cacheprovider
```

### 验收

- [x] 图包含真实动态循环，不是固定节点序列的包装。
- [x] 所有循环都有硬预算和 terminal route。
- [x] Agent 无法绕过 guard 直接进入 tool node。

## 11. Task 8 — Checkpointer、Runtime 和 Composition

### 依赖

- Modify: `pyproject.toml`
- Add `langgraph-checkpoint-sqlite` 的 LangGraph 1.2 兼容版本。
- PostgreSQL checkpointer 作为可选 production extra 或部署依赖，不强制本地开发安装。

### 文件

- Create: `src/data_agent/analysis_agent/checkpoints.py`
- Create: `src/data_agent/analysis_agent/runtime.py`
- Create: `src/data_agent/analysis_agent/composition.py`
- Modify: `src/data_agent/runtime/composition_root.py`
- Modify: `src/data_agent/runtime/contracts.py`（必要时）
- Create: `tests/integration/test_analysis_agent_runtime.py`
- Create: `tests/integration/test_analysis_agent_resume.py`

### 实现

- [x] 定义 CheckpointerFactory，测试用 InMemory、本地用 AsyncSQLite、生产可用 AsyncPostgres。
- [x] composition 生命周期负责 setup/close，不在模块 import 时建立连接。
- [x] `thread_id=run_id`；metadata 包含 tenant/user/conversation 的不可伪造映射。
- [x] `DataAnalysisAgentRuntime.run()` 适配公共 AsyncIterator[AgentEvent]。
- [x] Runtime 负责唯一 terminal/waiting event、异常安全化和资源关闭。
- [x] resume 从最新 checkpoint 加载并重新验证 principal、pins、run status。
- [x] 绑定 stale、checkpoint missing/corrupt、interrupt stale 返回类型化错误。
- [x] 新增 `build_analysis_agent_runtime()`，暂不替换 FastAPI 默认 factory。

### 恢复测试

- [x] InMemory pause/resume。
- [x] SQLite pause、关闭 composition、重建 composition、resume。
- [x] 重复 interrupt response 产生 conflict。
- [x] 其他 user/tenant resume 被拒绝。
- [x] source/binding 变化后 resume 失败。
- [x] 已取消或已完成 run 不能 resume。

### 验证

```bash
.venv/bin/python -m pytest \
  tests/integration/test_analysis_agent_runtime.py \
  tests/integration/test_analysis_agent_resume.py \
  tests/unit/analysis_agent \
  -q -p no:cacheprovider
```

### 验收

- [x] 服务重启后 run 可恢复。
- [x] checkpoint 无秘密和大型 artifact payload。
- [x] composition import inert。

## 12. Task 9 — API、运行控制、暂停/恢复与默认切换

### 文件

- Modify: `src/api/app.py`
- Modify: `src/api/routes.py`
- Modify: `src/api/run_streams.py`
- Modify: `src/api/schemas.py`
- Modify: `src/api/dataset_query_service.py`
- Modify: `tests/test_api_nl2sql.py`
- Modify: `tests/test_api_conversations.py`
- Modify: `tests/test_run_streams.py`
- Add: API resume tests

### 步骤

- [x] 先让 API 可以依赖注入旧 runtime 或新 Agent runtime。
- [x] 新增 `/api/runs/{run_id}/resume` 和 `/resume/stream`。
- [x] RunCoordinator 状态扩展为 running/waiting/completed/failed/cancelled。
- [x] waiting SSE 正常结束，run 保持可恢复。
- [x] replay 返回 waiting/resumed 后的完整单调事件序列。
- [x] 同一 conversation 只允许一个 active/waiting run；明确返回 conflict。
- [x] cancel 可以取消运行中 graph；waiting run cancel 后 checkpoint 标记不可恢复。
- [x] 将 `/api/nl2sql` 与 conversation message 的 source_id 分支统一改为 Agent Runtime。
- [x] composition 成功切换后删除 route 对 `DataSourceQueryService.run/stream` 的直接调用。
- [x] 保留 datasource management endpoints 和 DataSourceService。
- [x] 同步 `_status_for_response`、error mapping 和 safe internal response。

### 双跑验证

在默认切换前，测试环境对固定 fixture 同时调用旧 facade 和新 Agent：

- [x] 比较 logical refs、SQL AST digest、rows、chart 和主要 answer 数值；
- [x] 不要求自然语言和内部 trace 完全相同；
- [x] 任何 authority、安全或结果差异必须先解释并修复。

### 验证

```bash
.venv/bin/python -m pytest \
  tests/test_api_nl2sql.py \
  tests/test_api_conversations.py \
  tests/test_run_streams.py \
  tests/test_openapi_contract.py \
  tests/integration/test_analysis_agent_runtime.py \
  tests/integration/test_analysis_agent_resume.py \
  -q -p no:cacheprovider
```

### 验收

- [x] 默认请求真实进入新 Agent。
- [x] routes 不再编排 planner/compiler/connector。
- [x] pause/resume/cancel/replay 完整工作。

## 13. Task 10 — Conversation、Memory 与 Evidence 持久化

### 文件

- Modify: `src/data_agent/runtime/upload_runtime.py` 或抽取 ConversationRepository
- Modify: `src/data_agent/memory/contracts.py`
- Modify: `src/data_agent/memory/models.py`
- Modify: `src/data_agent/memory/providers/postgres.py`
- Modify/Add: conversation and memory tests

### 步骤

- [x] 将 UploadDatasetRuntime 中会话 CRUD 与“无 datasource 拒绝”职责拆开。
- [x] 新 Agent Runtime 委托 ConversationRepository，API 不再手工 `_record_conversation_turn`。
- [x] 完成 run 时原子保存 user/assistant messages、plan summary、steps、evidence、pins 和 trace。
- [x] waiting run 不写最终 assistant message。
- [x] resume 完成后只写一个最终 turn，防止重复消息。
- [x] follow-up 只加载 bounded conversation summary 和必要 prior evidence。
- [x] 保留 Memory proposal/approval/commit；Agent 只能 propose。
- [x] checkpoint 与长期 Memory 严格分离。

### 验证

```bash
.venv/bin/python -m pytest \
  tests/test_api_conversations.py \
  tests/unit/memory \
  tests/integration/test_memory_postgres.py \
  tests/integration/test_analysis_agent_runtime.py \
  -q -p no:cacheprovider
```

### 验收

- [x] 消息无重复，跨用户隔离。
- [x] follow-up 有上下文但不继承旧 run 临时状态。
- [x] Agent 不能直接写 approved Memory。

## 14. Task 11 — 前端 Agent 执行体验

### 注意

当前 `frontend/src/relationships/RelationshipGraphEditor.tsx`、`relationshipGraphState.*` 和 `styles.css` 有用户改动。编辑 `styles.css` 前必须检查并保留这些 diff；优先新增独立 CSS 区块和组件文件。

### 文件

- Create: `frontend/src/agent/AgentRunPanel.tsx`
- Create: `frontend/src/agent/AgentPlanView.tsx`
- Create: `frontend/src/agent/AgentStepItem.tsx`
- Create: `frontend/src/agent/AgentInputCard.tsx`
- Create: `frontend/src/agent/agentRunState.ts`
- Create: corresponding Vitest tests
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css` carefully

### 步骤

- [x] 实现 AgentEvent reducer，支持乱序拒绝、重复幂等和 replay hydration。
- [x] 展示 plan revision、step status、工具业务名称和安全 observation summary。
- [x] run_waiting 显示 clarification/approval card。
- [x] resume 使用新 API，支持普通和 stream。
- [x] 刷新后从 conversation + run replay 重建等待/执行状态。
- [x] cancel 与 waiting cancel 有明确交互。
- [x] Evidence Drawer 展示多 artifact/evidence，而不是只有单 SQL。
- [x] 保留最终 answer/chart/table 的业务优先级。
- [x] 不展示 Prompt、chain-of-thought、credential、内部路径或未脱敏 payload。
- [x] 移动端完整可操作。

### 验证

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

### 验收

- [x] 多步 run、waiting、resume、cancel、replay 均有 UI 测试。
- [x] 当前 datasource/relationship graph 功能没有回归。
- [x] 用户现有样式改动被保留。

## 15. Task 12 — 多步轨迹、结果级黄金评测与安全门禁

### 文件

- Create: `tests/integration/test_analysis_agent_trajectory.py`
- Create: `tests/integration/test_analysis_agent_security.py`
- Create: `tests/fixtures/analysis_agent_cases.json`
- Create/Modify: `tests/support/` Agent evaluators
- Migrate useful cases from OList golden fixtures

### 必需轨迹用例

- [x] 单查询聚合：Agent 不产生明显冗余工具调用。
- [x] 多查询比较：分别取得两个结果再综合。
- [x] 趋势 + 异常：query + compute + chart + evidence。
- [x] 空结果：观察后修改过滤条件并重规划。
- [x] schema ambiguity：请求澄清。
- [x] relationship ambiguity：pause/resume 后按用户选择继续。
- [x] preview 模式：只基于预览并声明限制。
- [x] budget exhaustion：安全终止且无后续工具调用。
- [x] cancellation：工具取消、无 terminal duplication。
- [x] restart resume：SQLite checkpoint 恢复。

### 安全用例

- [x] 数据值 prompt injection。
- [x] 模型返回未知工具或任意 SQL。
- [x] 模型尝试改变 source/binding/tenant/user。
- [x] artifact 跨 run/跨 tenant。
- [x] stale interrupt replay。
- [x] malicious identifiers 和 provider error redaction。
- [x] checkpoint/artifact 中扫描 credential markers。

### 评测不变量

- [x] answer 关键数值与 oracle/result artifact 一致。
- [x] evidence pins 与实际 source/binding 一致。
- [x] 轨迹只使用允许工具并处于预算内。
- [x] SQL AST 只读且 relation allowlist 正确。
- [x] 不强制唯一自然语言或唯一行动顺序。

### 验证

```bash
.venv/bin/python -m pytest \
  tests/integration/test_analysis_agent_trajectory.py \
  tests/integration/test_analysis_agent_security.py \
  tests/e2e \
  -q -p no:cacheprovider
```

### 验收

- [x] 新 Agent 覆盖旧单查询黄金能力。
- [x] 至少三个用例必须依赖真实多步工具轨迹。
- [x] 全部安全不变量通过。

## 16. Task 13 — 更新 Studio、CLI、文档与生成契约

### 文件

- Modify: `src/data_agent/adapters/studio.py`
- Modify: `langgraph.json`
- Modify: `src/data_agent/cli.py`
- Modify: `README.md`
- Modify: `docs/project_reading_guide.md`
- Modify: `docs/apifox-openapi.json`（通过脚本生成）
- Modify: `frontend/src/generated/*`（通过脚本生成）
- Modify: packaging/contract tests

### 步骤

- [x] Studio 直接导出新 compiled Agent graph；不再用单节点 Runtime wrapper 冒充完整图。
- [x] Studio 使用测试/开发 composition，不在 import 时连接生产数据源。
- [x] CLI 的 plan/preview/execute 使用相同 Agent Runtime。
- [x] README 更新默认主链路、配置、checkpoint、resume 和验证命令。
- [x] Reading guide 从 Agent Runtime 开始，明确 legacy cleanup 后的层次。
- [x] 导出 OpenAPI、Apifox 和 AgentResponse 前端 schema。
- [x] 已审阅 `pyproject.toml` package data；退役 Pack data-files 按约束保留到 Task 14 完成引用与 wheel 审计后再删除。

### 生成命令

```bash
python scripts/export_apifox_openapi.py
python scripts/export_frontend_agent_response_schema.py
```

### 验证

```bash
.venv/bin/python -m pytest \
  tests/test_studio_adapter.py \
  tests/test_main_cli.py \
  tests/test_openapi_contract.py \
  tests/test_frontend_agent_response_schema.py \
  tests/contract/test_src_packaging.py \
  -q -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
```

### 验收

- [x] API、CLI、Studio 使用同一 Agent Graph 和 Runtime contracts。
- [x] 所有生成契约新鲜。
- [x] 文档不再把 DataSourceQueryService 或 InternalGraphExecutor 描述为默认链路。

## 17. Task 14 — 证明并删除旧实现、无用目录、文件和依赖

### 17.1 删除原则

本 Task 是正式交付的一部分，不是可选“顺手清理”。但每项删除必须满足：

1. replacement 已实现并通过对应测试；
2. `git ls-files`、Python import 审计、配置/脚本字符串引用和打包清单均已检查；
3. removal manifest 标记为 `approved-to-delete`；
4. 有明确恢复方式：Git 恢复、重新构建或重新安装；
5. 删除后 focused + full verification 通过。

### 17.2 建立可复现引用审计

- [x] Create: `scripts/audit_repository_reachability.py`。
- [x] 脚本解析 Python AST imports，并从以下根入口做可达性分析：

```text
pyproject.toml project.scripts
src/api/app.py
main.py
langgraph.json graph entry
scripts/ 中保留的命令入口
pytest 收集到的测试模块
frontend/src/main.tsx
frontend package.json scripts
```

- [x] 静态 import 结果只是证据之一；对动态 import、package data、YAML/JSON 路径、subprocess 命令和文档生成器做显式 reference scan。
- [x] 输出报告到临时目录或 `generated/reports/`，不要把本机绝对路径写入提交。
- [x] 对每个候选项运行精确 `rg`，记录引用者和迁移状态。
- [x] 检查 wheel `RECORD`/内容，确认删除项不会被打包。

### 17.3 第一批：明确可重建的本地垃圾

在 manifest 确认不是用户资产后，显式删除：

```text
所有 __pycache__ 目录
.pytest_cache/
build/
dist/
nl2sql_langgraph.egg-info/
src/data_agent.egg-info/
frontend/dist/
generated/runtime-path-tests/
generated/runtime-source-tests/
docs/superpowers/.DS_Store
```

以下项目要单独判断：

- `frontend/node_modules/` 与 `.pnpm-store/`：若需要节省仓库空间且可从 lockfile 重建则删除；随后用选定 package manager 重新安装验证。不要同时维护 npm 与 pnpm 的无意双缓存。
- `var/data-agent/`：只删除明确属于本地测试/演示的 state；若包含用户实际导入快照，先停止并请求确认，不得当缓存删除。
- `.env.codex-backup-*`、`.kaggle/`：可能包含秘密。不得读取内容；确认属于无用本地备份后按显式路径删除，并提醒不可通过 Git 恢复。
- `.impeccable/`：若只属于已完成设计工具临时产物且没有代码/文档引用则删除；否则在 manifest 标记保留理由。

根目录 `api/`、`catalog/`、`core/`、`engine/`、`graph/`、`rag/` 若审计确认只包含 `__pycache__` 或已迁移残留，删除空目录。目录本身无需在 Git 中提交删除，但清理结果应记录。

### 17.4 第二批：退役默认 Workflow

新 Agent 默认切换且双跑测试稳定后：

- [x] 删除 `src/api/dataset_query_service.py` 中 orchestration facade；若文件已无其他职责则删除整个文件。
- [x] 删除 routes/app 对 `data_source_query_service` 的注入和分支。
- [x] 删除仅验证该 facade 内部实现的测试；结果级用例迁移到 Agent tests。
- [x] 删除已迁移模型的重复定义和兼容 re-export。
- [x] 清理 API schema 中废弃字段/别名，但保留设计明确的 response convenience fields。

### 17.5 第三批：退役固定 Pack/OList 兼容链路

只有 Task 12 已把必要黄金结果迁移到新 Agent 后才能执行。逐项审计并删除仅服务旧链路的候选：

```text
src/data_agent/execution/spec.py 中 COMMERCE_EXECUTION_GRAPH
src/data_agent/execution/langgraph_adapter.py（若新 Studio/Runtime 不再引用）
src/data_agent/execution/executor.py、compiler.py、models.py、contracts.py 中仅固定 GraphSpec 使用的部分
src/data_agent/runtime/bundle_store.py
src/data_agent/runtime/packs.py
src/data_agent/runtime/paths.py
src/data_agent/runtime/profile_loader.py
src/data_agent/runtime/context_resolver.py 中仅 Pack 使用的部分
src/data_agent/runtime/service.py 中 DefaultDataAgentRuntime Pack 实现
src/data_agent/runtime/composition_root.py 中 build_runtime/build_olist_runtime
src/data_agent/skills/commerce/ 中仅 Pack 使用的能力
src/data_agent/tools/providers/ 中旧六工具 Pack providers/registry
packs/domains/commerce/
packs/enterprises/olist/
packs/deployments/olist-local.yaml
generated/bundles/olist-local.json
generated/semantic/commerce.json
schema_catalog.json（若无新测试/默认 runtime 需要）
```

删除前必须先迁移仍被复用的通用类型。例如 `PreparedQuery`、`QueryParameter` 若仍在 dataset Agent 使用，应移动到 `src/data_agent/dataset_query/` 或 connector contract，再删除旧 `runtime.binding` 中的重复实现。

对应删除或改写：

```text
scripts/compile_packs.py
scripts/rebuild_semantic_index.py
scripts/import_olist_dataset.py
scripts/refreeze_olist_golden_results.py
scripts/run_olist_frontend_eval*.mjs
scripts/seed_olist_eval_auth.py
tests/e2e/test_olist_golden_runtime.py
tests/contract/test_commerce_domain_pack.py
tests/contract/test_olist_enterprise_binding.py
tests/contract/test_pack_models.py
tests/fixtures/olist_*
tests/support/olist_*
docs 中仅描述已删除 Pack/OList 运行方式的过时文档
```

不是列表中的所有项都必然删除；以最终 reachability 和新评测引用为准。manifest 必须为每项给出 retain/migrate/delete 结论。

### 17.6 第四批：依赖、打包与前端死代码

- [x] 用 Python import/reference + wheel 检查审计 `pyproject.toml` dependencies、dev extras 和 package data。
- [x] 删除只服务已退役 Pack/Graph 的依赖与 data-files 项。
- [x] 保留新 Agent 实际需要的 `langgraph`、checkpointer、provider adapter、SQLGlot、connector 和 Pydantic 依赖。
- [x] 审计 `frontend/src` 未引用组件、旧 event reducer、旧单查询专用状态和 CSS selector。
- [x] 删除无引用 TypeScript exports、测试 fixture 和 CSS；重新运行 TypeScript build。
- [x] 审计 README/docs 中指向已删除文件的链接。
- [x] 清理 `.gitignore` 中只服务已删除工具/目录的规则，同时保留缓存、state、secret 和构建产物规则。

### 17.7 历史文档处理

- [x] 设计/实施完成后，检查 `docs/superpowers/specs` 与 `plans`。
- [x] 新设计仍引用的历史决策文档保留，并在顶部标记 superseded/retained reason。
- [x] 仅描述已删除代码、没有审计或历史价值且已被本设计完整替代的文档列入 manifest 后删除。
- [x] 不保留会误导新开发者按旧主链路实施的“当前架构”文档。
- [x] 更新 `docs/project_reading_guide.md` 为唯一推荐阅读入口。

### 17.8 每批删除后的验证

```bash
.venv/bin/python -m pytest -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

另外：

- [x] 在临时 venv 中安装 wheel，运行 `data-agent --help`、API import、Agent graph import。
- [x] 检查 wheel 内不包含已删除 Pack/OList/generated 文件。
- [x] 运行 `rg` 检查没有失效 import、路径和文档链接。
- [x] 运行 repository reachability audit，所有剩余 orphan 都有 retain reason。
- [x] `git status --short` 中只有本任务预期变更和原有用户改动。

### 验收

- [x] removal manifest 每项有最终结论和证据。
- [x] 当前及目标架构均不使用的 tracked 文件已删除。
- [x] 可重建缓存/构建产物不留在项目目录或 Git。
- [x] 无宽泛或误删用户数据的操作。
- [x] 删除后全量测试、构建和 wheel 安装通过。

## 18. Task 15 — 最终发布验证与交接

### 全量验证

```bash
.venv/bin/python -m pytest -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

### 必查事项

- [x] 运行 API health/login/datasource upload/binding/agent question 的端到端 smoke test。
- [x] 分别验证 CSV、XLSX、SQLite、PostgreSQL。
- [x] 分别验证 Plan、Preview、Execute。
- [x] 验证多步分析、重规划、pause/restart/resume、cancel/replay。
- [x] 检查 final response 中 plan、steps、evidence、pins、rows/chart。
- [x] 搜索 event/checkpoint/artifact metadata，不含 credential、DSN、内部路径或 chain-of-thought。
- [x] 检查跨 tenant/user 拒绝。
- [x] 检查 README/reading guide/OpenAPI/Apifox/前端 schema。
- [x] 检查 removal manifest 和删除报告完整。
- [x] 检查没有意外修改用户原有 relationship editor changes。

### 最终交接报告必须包含

```text
1. 默认主链路最终架构
2. 新增/修改/删除文件摘要
3. 删除项及其可恢复性
4. 数据库与 state migrations
5. 公开 API/事件变更
6. 测试和构建命令的准确结果
7. 未完成项或已知风险
8. 本地运行与生产配置方法
9. 回滚步骤
```

## 19. Task 16 — 将当前实现分支安全合并到 main

本 Task 只在 Task 0–15 全部完成、清理门禁通过后执行。当前已知开发分支为 `Agent`，但执行时必须重新读取实际分支名，不能假设分支未变化。

### 19.1 合并前置条件

完成说明：主工作区仍有四个 Task 0 已记录、不可擅自处理的未跟踪本地项。实际执行时确认源分支已跟踪树干净、提交边界完整，并在全新的隔离 `main` worktree 中完成合并与复验；未通过 ignore、stash、删除或提交来隐藏这些本地项。

- [x] 确认当前不在 `main`：

```bash
git branch --show-current
```

- [x] 确认所有实现文件、删除和生成契约已经被有意纳入提交。
- [x] 确认源分支已跟踪树干净，且用于合并的隔离 worktree 中 `git status --short` 为空；没有把 `.env*`、credential、本地 state、artifact、node_modules、缓存或用户未确认的临时文件提交进去。
- [x] 用户原有产品改动已经审查并纳入源提交；四个不属于产品提交的受保护本地项保留在主工作区，隔离 worktree 方案避免了 stash、reset、删除或忽略它们。
- [x] 在源分支重新运行 Task 15 全量门禁并记录源分支 HEAD：

```bash
git rev-parse HEAD
git status --short
.venv/bin/python -m pytest -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

- [x] wheel 验证生成的 `dist/` 必须按 removal manifest 清理，确保切换分支前工作树再次为空。
- [x] 检查即将合并的提交和 diff 范围：

```bash
git log --oneline --decorate main..HEAD
git diff --stat main...HEAD
git diff --check main...HEAD
```

- [x] 如果 diff 中出现秘密、用户数据、大型二进制、缓存或计划外目录，停止合并并修正源分支。

### 19.2 同步 main

- [x] 记录合并前 `main` 和源分支 commit ID，写入最终交接报告。
- [x] 若配置了 `origin`，先运行：

```bash
git fetch origin
```

- [x] 检查本地 `main`、`origin/main` 和源分支关系。不得使用 `git reset --hard`、force push 或覆盖本地 main 的独有提交。
- [x] 切换到本地 main：

```bash
git switch main
```

- [x] 如果 `origin/main` 存在且本地 main 只是落后，使用 fast-forward-only 同步：

```bash
git merge --ff-only origin/main
```

- [x] 如果本地 main 与 origin/main 已分叉，停止并报告 commit 图，不自行 rebase、reset 或选择一侧覆盖。

### 19.3 执行合并

- [x] 使用明确的 merge commit 合并源分支，保留整项迁移的审计边界：

```bash
git merge --no-ff Agent -m "merge: migrate default runtime to data analysis agent"
```

执行时将 `Agent` 替换为 19.1 记录的实际源分支名。

- [x] 如果无冲突，继续 19.4。
- [ ] 如果有冲突：
  - 逐文件查看 source/main 两侧意图；
  - 特别保护用户原有 relationship editor 和样式改动；
  - 不使用 `checkout --theirs`/`--ours` 批量覆盖；
  - 解决后运行相关 focused tests，再完成 merge commit；
  - 若不能确定正确结果，执行 `git merge --abort`，回到源分支报告冲突，不猜测。

### 19.4 在 main 上重新验证

- [x] 确认源分支 HEAD 已成为 main 的祖先：

```bash
git merge-base --is-ancestor <SOURCE_HEAD> main
```

- [x] 查看合并图和工作树：

```bash
git log --graph --oneline --decorate -n 30
git status --short
git diff --check HEAD^ HEAD
```

- [x] 在 main 上重新运行完整门禁：

```bash
.venv/bin/python -m pytest -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

- [x] 在临时 venv 安装 main 构建的 wheel，验证 CLI、API import 和 Agent graph import。
- [x] 运行最小端到端 smoke：登录、数据源、binding、Agent execute、pause/resume、run replay。
- [x] 清理本次验证产生的、manifest 已批准的构建产物，再确认 main 工作树为空。

### 19.5 合并后的处理和回滚

- [x] 最终报告记录：源分支名、SOURCE_HEAD、合并前 MAIN_HEAD、merge commit、验证结果。
- [x] 不自动删除源分支；待用户确认 main 运行正常后再决定是否删除。
- [x] “并入 main”默认只要求本地 main 合并成功。除非用户另外明确要求，不自动 push `main`、创建 PR、打 tag 或删除远端分支。
- [x] 若 main 合并后发现问题，不使用 reset/force push。已共享历史优先使用 `git revert -m 1 <MERGE_COMMIT>`；尚未共享时也先报告并征得用户确认再选择回滚方式。

### 验收

- [x] 源分支完整历史可从 main 到达。
- [x] main 上完整测试、前端构建、wheel 安装和 smoke tests 通过。
- [x] main 工作树干净，无秘密、缓存或本地 state 被提交。
- [x] 最终报告包含可审计 commit ID 和回滚说明。

## 20. 建议提交拆分

不要把整个迁移压成一个提交。建议保持以下可审查提交边界：

1. `agent contracts and typed events`
2. `artifact store and dataset tool authority`
3. `extract deterministic dataset query services`
4. `dataset agent tool registry`
5. `planner evaluator synthesizer`
6. `langgraph analysis agent runtime`
7. `checkpoint resume and API integration`
8. `frontend agent run experience`
9. `agent trajectory and security evals`
10. `switch default runtime and update docs`
11. `remove legacy workflow and pack runtime`
12. `clean generated artifacts and unused dependencies`

每个提交都必须自洽并通过对应 focused tests。只有用户明确要求时才执行 commit/push/PR。

## 21. 下一会话启动 Prompt

将以下内容连同本设计和实施计划交给新的实现会话：

```text
请在 /Users/yehj/project/NL2SQL 中实施 Data Analysis Agent 迁移。

开始前完整阅读：
1. docs/superpowers/specs/2026-08-08-data-analysis-agent-design.md
2. docs/superpowers/plans/2026-08-08-data-analysis-agent-implementation.md
3. docs/project_reading_guide.md
4. README.md

严格从实施计划 Task 0 开始，维护复选框。当前工作区已有用户未提交修改，尤其是 frontend/src/relationships/ 与 frontend/src/styles.css；不得 reset、覆盖或清理这些修改。

核心边界：模型不能直接执行任意 SQL 或代码；所有工具调用经过 Registry/Invoker、authority、budget、SQLGlot compiler 和只读 connector。使用原生 LangGraph StateGraph 实现 plan → guard → tool → observe → evaluate → replan 循环，并实现 durable checkpoint、pause/resume、typed SSE 和 evidence grounding。

仓库清理是正式范围：先维护 repository-removal-manifest，通过引用审计、测试、构建和 wheel 检查证明无用后，再分批删除旧 Workflow、仅 Pack/OList 使用的兼容代码、生成物、缓存、依赖和过时测试/文档。不要使用 git clean -fdx 或宽泛递归删除；不得误删用户数据和秘密文件。

每个 Task 先写失败测试，再实现，再运行计划中的 focused verification。遇到设计未覆盖的重大产品选择时停止并说明，不要自行放宽权限或安全边界。

Task 0–15 全部完成后执行 Task 16：先确保源分支提交完整且工作树干净，在源分支跑全量门禁；再同步本地 main，使用明确 merge commit 合并源分支，并在 main 上重新运行全量测试、前端构建、wheel 安装和端到端 smoke。不要 reset、force push、自动删除源分支或在未明确要求时 push main。
```
