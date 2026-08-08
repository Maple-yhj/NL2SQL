# Data Agent OList Commerce Platform Migration Implementation Plan

> **Historical / superseded:** Retained as Pack-platform provenance. The fixed Commerce/OList Pack runtime and assets were retired after the 2026-08-08 reachability and wheel audit.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将当前 NL2SQL Agent 一次性升级为配置驱动的 Data Agent 平台。首个版本只实现 Commerce 领域、OList PostgreSQL Enterprise Data Binding，并允许不兼容旧行为及调整目录结构。

**Architecture:** 模型只生成不含物理表字段的 LogicalQueryPlan；Commerce Domain Pack 提供规范语义；OList Binding 将规范语义绑定到数据库；单一 Execution Graph 通过受控 Tool Registry 完成查询；Data Agent Runtime 统一负责上下文、预算、事件、Memory 和产品入口。

**Tech Stack:** Python 3.12、Pydantic v2、PyYAML、LangGraph、asyncpg、sqlglot、PostgreSQL、FastAPI、React、TypeScript、unittest、Vitest。

## 1. 实施边界

- 本次为一次性破坏性迁移，不保留旧 Python API 兼容层。
- 首期 Domain 只实现 commerce。
- 首期 Enterprise Data Binding 只实现 olist。
- 首期 Skill 只实现 commerce.analytics。
- 首期数据库只支持 PostgreSQL，只允许只读查询。
- 最终只保留一套 Execution Graph，不再保留 fixed/dynamic 或 agent_mode。
- Domain Pack 和 Skill 禁止出现 OList 表名、列名或原始 SQL。
- Enterprise Binding 可以声明数据库、表、列、关系、类型转换和策略引用，但不能携带任意 SQL、Python、Jinja、shell 或动态 import。
- Secret 只以引用形式存在配置中，明文只在 Runtime 启动时解析。
- 旧架构可以在迁移中短暂共存，但只有新 Runtime 通过 48 条 OList 验收后才能删除。

## 2. 目标目录

~~~text
src/
  data_agent/
    runtime/
    skills/
      commerce/
    execution/
    tools/
      connectors/
      providers/
    memory/
      providers/
    adapters/
    cli.py
  api/

packs/
  schemas/v1/
  domains/commerce/
  enterprises/olist/
  deployments/olist-local.yaml

generated/
  bundles/

tests/
  unit/
  contract/
  integration/
  e2e/
  support/
  fixtures/

frontend/
scripts/
db/
~~~

迁移完成后删除 graph、engine、catalog、rag、core 目录，不创建 re-export 或转发模块。

## 3. 核心模块职责

### Data Agent Runtime

- 唯一 Agent 应用入口。
- 固化请求、身份、预算和配置版本。
- 加载不可变 ResolvedRuntimeBundle。
- 驱动 Skill、Execution Graph、Tool Registry 和 Memory。
- 输出流式 AgentEvent 与最终 AgentResponse。
- 统一处理超时、取消、错误码、trace、lineage 和资源关闭。

### Skill System

- 首期只注册 commerce.analytics。
- 维护 Commerce 的规划提示、逻辑计划模型和确定性验证器。
- 只使用规范指标、实体、字段和关系。
- 覆盖指标、趋势、多维、Top-N、明细、支付、评价、物流和窗口分析。
- 不负责数据库连接、物理字段映射或权限执行。

### Execution Graph

- 全系统只编译一套 Commerce 执行图。
- 包含 Memory 召回、语义检索、上下文组装、规划、绑定、校验、成本检查、预览、执行、结果验证、回答和 Memory proposal。
- plan、preview、execute 通过条件分支实现，不复制图。
- 纠错必须按错误类型路由，并受次数、耗时和工具预算限制。
- LangGraph 只是执行后端，不向 Skill、Tool 或 API 暴露内部类型。

### Tool Registry

- 统一 ToolSpec、Provider、输入输出 Schema、权限、预算、超时、审计和脱敏。
- Skill 只允许调用以下六个工具：

  - semantic.search
  - data.inspect
  - query.compile
  - query.execute
  - result.profile
  - answer.render

- 所有调用必须经过同一个 ToolInvoker。
- 数据库执行必须校验只读、relation allowlist、seller scope、行数上限和 statement timeout。

### Memory

- 区分 Working、Conversation、User、Episodic、Enterprise Memory。
- Domain Pack、OList Binding 和权限策略不属于 Memory。
- Enterprise 与共享 Memory 使用 propose、policy check、approval、commit。
- PostgreSQL 是权威存储；Graphiti 仅作为可选检索 Provider。
- 默认不保存完整查询结果、Secret 或未脱敏参数。

### Enterprise Data Binding

- Domain Pack 定义可跨企业复用的 Commerce 规范语义。
- OList Binding 只负责把规范实体、字段和关系映射到 OList 物理结构。
- Deployment Profile 只选择 Enterprise Binding、Secret 引用和运行限额。
- 新企业原则上只新增 Binding 和 Deployment，不修改 Commerce Skill。

## 4. 实施阶段

### Task 1：建立 src 布局与平台公共契约

主要工作：

- 将 Python 包切换为 src layout。
- 建立 AgentRequest、PrincipalContext、RunBudget、AgentEvent、AgentResponse。
- 建立 Domain Pack、Enterprise Binding、Deployment Profile、ResolvedRuntimeBundle。
- 建立稳定错误码和公共 Protocol。
- 更新 pyproject.toml 的包发现和依赖；CLI entrypoint 在 Task 9 创建 CLI 模块时注册。

主要目录：

- src/data_agent/runtime/
- packs/schemas/v1/
- tests/unit/runtime/
- tests/contract/

完成门槛：

- Pydantic Schema 可稳定导出。
- Bundle 编译结果可重复产生相同 digest。
- 配置拒绝明文凭据和未声明字段。

### Task 2：实现 Commerce Domain Pack

主要工作：

- 定义九个规范实体：Order、OrderItem、Customer、Seller、Product、Payment、Review、GeoLocation、CategoryTranslation。
- 定义实体字段、粒度、八条关系、词汇和领域规则。
- 定义四个首期指标：

  - commerce.gmv
  - commerce.order_count
  - commerce.average_item_price
  - commerce.average_review_score

- 将现有 48 条 OList 问题迁移为物理无关的 Commerce 逻辑评测。

主要目录：

- packs/domains/commerce/
- tests/contract/test_commerce_domain_pack.py

完成门槛：

- Domain Pack 中不存在 OList relation、column、schema 或 SQL。
- 48 条领域评测均使用 canonical Commerce ID。

### Task 3：实现 OList Enterprise Data Binding

主要工作：

- 映射九张 OList 表及其字段。
- 定义关系键、类型、时区、grain 和 relation allowlist。
- 定义 admin 与 seller_id 租户范围。
- 建立 olist-local Deployment Profile。
- 使用 schema_catalog.json 校验表、列、类型和 schema fingerprint。
- 生成 pack.lock 和不可变 Bundle。

主要目录：

- packs/enterprises/olist/
- packs/deployments/olist-local.yaml
- generated/bundles/
- tests/contract/test_olist_enterprise_binding.py

完成门槛：

- 九个实体映射完整。
- seller scope 路径可验证。
- Binding 不包含任意 SQL 或可执行代码。
- Schema 漂移会阻止 Bundle 发布。

### Task 4：实现 commerce.analytics Skill

主要工作：

- 实现 SkillManifest、Skill Registry 和 Commerce Skill。
- 定义 typed LogicalQueryPlan。
- LogicalQueryPlan 支持指标、维度、过滤、时间范围、排序、limit、派生计算、HAVING、窗口分析和安全 grain alignment。
- 实现 canonical ID、关系、粒度、fan-out 和结果形态验证。
- 模型输出中出现物理标识符或 SQL 时直接拒绝。

主要目录：

- src/data_agent/skills/
- src/data_agent/skills/commerce/
- tests/unit/skills/

完成门槛：

- Skill Registry 只包含 commerce.analytics。
- LogicalQueryPlan 不包含物理数据库信息。
- 现有 OList 复杂问题可表达，不需要退回原始 SQL 字符串。

### Task 5：实现 Tool Registry 与 PostgreSQL 数据工具

主要工作：

- 实现 ToolSpec、ToolCall、ToolResult、ToolRegistry、ToolInvoker。
- 实现原子工具预算、Schema 校验、超时、异常映射、trace 和脱敏。
- 实现短期 AccessGrant 与 CredentialBroker。
- 实现 asyncpg 连接池和只读 PostgreSQL Connector。
- 实现六个 Tool Provider。
- 使用 sqlglot 完成 LogicalQueryPlan 到 BoundQueryPlan、PreparedQuery 和 PostgreSQL AST 的确定性编译。

主要目录：

- src/data_agent/tools/
- src/data_agent/tools/connectors/
- src/data_agent/tools/providers/
- src/data_agent/runtime/binding.py
- tests/unit/tools/
- tests/integration/

完成门槛：

- Tool manifest 与真实 Provider 一致。
- 非 SELECT、越权 relation、缺失 seller scope、超限和超时均被阻止。
- plan 模式不访问数据库。
- preview 与 execute 使用相同 PreparedQuery 和策略决策。

### Task 6：实现单一类型化 Execution Graph

主要工作：

- 定义 Node、Edge、Artifact、Error Route 和 Execution Context。
- 从 Runtime 骨架与 Commerce Skill fragment 编译唯一图。
- 实现 plan、preview、execute 分支。
- 实现查询静态校验、EXPLAIN/成本门槛、预览验证和正式执行。
- 实现有界纠错、checkpoint 和 replay。
- 实现内部执行器与 LangGraph 后端的一致性测试。

主要目录：

- src/data_agent/execution/
- tests/unit/execution/
- tests/integration/test_execution_graph.py

完成门槛：

- 图中不存在无界循环。
- ACCESS_DENIED、SQL_POLICY_VIOLATION、BINDING_STALE 不会放宽权限或盲目重试。
- 编译错误、逻辑错误、成本错误和结果错误按各自策略路由。
- 两个执行后端产生相同工具顺序和最终 Artifact。

### Task 7：实现 Memory 与会话持久化

主要工作：

- 定义 Memory Scope、Record、Candidate、Query、Budget、Approval 和 Checkpoint。
- 实现 MemoryManager、Null Provider、PostgreSQL Provider、可选 Graphiti Provider。
- 建立 conversation、message、artifact reference、memory proposal 和 checkpoint 表。
- 将会话读写统一到 tenant_id、user_id、conversation_id 所有权边界。
- 使用事务保存用户消息、AgentResponse、会话摘要和 Memory proposal。

主要目录：

- src/data_agent/memory/
- db/data_agent_memory.sql
- tests/unit/memory/
- tests/integration/test_memory_postgres.py

完成门槛：

- 未审批共享 Memory 不可召回。
- 跨用户、跨租户会话访问被拒绝。
- 事务失败不产生半条会话记录。
- 完整结果集不写入 Memory 或 message JSON。

### Task 8：组装 Data Agent Runtime

主要工作：

- 实现 BundleStore、SecurityContext、ContextEnvelope、ContextAssembler。
- Context 优先级固定为 Security、Domain、Binding、Skill、Approved Memory、Conversation、Execution Evidence。
- ContextAssembler 保持纯函数；Memory 和 semantic.search 由 Execution Graph 调用。
- 实现 RuntimeDependencies、组合根、生命周期和资源关闭。
- 实现 DataAgentRuntime.run 及事件转换。
- 为每次运行固化 Bundle、Skill、Graph、Tool 和模型版本。

主要目录：

- src/data_agent/runtime/
- scripts/compile_packs.py
- scripts/rebuild_semantic_index.py
- tests/unit/runtime/
- tests/integration/test_data_agent_runtime.py

完成门槛：

- API、CLI 和 Adapter 只通过 Runtime 执行 Agent。
- 同一请求期间 Bundle 不热切换。
- 超时、取消和失败均产生稳定错误码及安全响应。
- include_trace=false 时不向客户端返回 trace，但内部审计仍保留。

### Task 9：切换 API、CLI、LangGraph Studio 与前端

主要工作：

- 将 api 移入 src/api。
- FastAPI lifespan 创建并关闭一个 Runtime composition。
- 保留认证、会话、提问能力，但请求改为 mode=plan|preview|execute。
- 移除 agent_mode、fixed/dynamic、intent 响应字段。
- 新产品响应使用 logical_plan、sql、rows、answer、trace 和 pending_memory_updates。
- 创建可安装的 data-agent CLI。
- LangGraph Studio 只包装 DataAgentRuntime，不直接暴露内部 Execution Graph。
- 前端请求和 ViewModel 迁移到新契约。
- 重新生成 docs/apifox-openapi.json。

主要目录：

- src/api/
- src/data_agent/cli.py
- src/data_agent/adapters/
- frontend/src/
- main.py
- langgraph.json

完成门槛：

- API、CLI、Studio 和前端均不 import 旧 graph.pipeline。
- agent_mode 请求返回校验错误，不被静默忽略。
- 后端测试、前端测试和 production build 通过。
- 安装后的 CLI 可运行 help 和 validate-config。

### Task 10：完成 48 条验收并删除旧架构

主要工作：

- 使用相同 DataAgentRuntime 执行 48 条 Commerce/OList golden cases。
- CI 使用确定性模型和 Connector fixture；真实模型/数据库作为独立发布检查。
- 验证 logical plan、binding、policy、SQL 编译、seller scope、结果和答案证据。
- 在新 Runtime 达到 48/48 后删除旧目录及旧专属测试。
- 迁移 OList 导入、OpenAPI、认证 seed 和前端评测脚本。
- 更新 README.md、summary.md、docs/project_reading_guide.md 和 .env.example。
- 构建 wheel 并执行安装后 smoke test。

删除范围：

- graph/
- engine/
- catalog/
- rag/
- core/
- 已被新 Memory/Connector 替代的旧数据库代码
- 只验证旧实现的测试

完成门槛：

- OList golden eval 48/48。
- 全部 backend tests 通过。
- 全部 frontend tests 与 production build 通过。
- Pack Schema 和 Bundle 均为最新。
- 生产代码中不存在 graph、engine、catalog、rag、core、run_nl2sql、agent_mode 引用。
- Wheel 安装、API import、CLI validate-config 通过。

## 5. 执行顺序与提交策略

严格按 Task 1 到 Task 10 执行。依赖关系为：

~~~text
公共契约
→ Commerce Domain Pack
→ OList Binding
→ Commerce Skill
→ Tool Registry
→ Execution Graph
→ Memory
→ Runtime
→ 产品入口切换
→ 48/48 与旧架构删除
~~~

每个 Task：

1. 先补该阶段的失败测试。
2. 实现最小闭环。
3. 运行阶段测试。
4. 在 Task 4、6、8、9、10 后运行全量测试。
5. 只提交该阶段相关文件。

不在本计划中提前锁死的细节包括：具体 Pydantic 字段拆分、测试 double 的实现形式、SQL AST 内部类层次、Graph 节点的最终命名、DDL 索引细节和 UI 呈现细节。这些在对应 Task 实施时根据失败测试和现有代码决定，但不得突破本计划的模块边界与验收门槛。

## 6. 最终验收命令类别

执行阶段应提供并通过以下类别的命令：

- Pack Schema export/check。
- OList Bundle compile/check。
- Backend unittest 全量测试。
- OList 48-case deterministic E2E。
- Frontend Vitest。
- Frontend production build。
- Legacy import/symbol boundary check。
- Wheel build、安装和 CLI/API smoke test。

具体命令参数在各 Task 实施时根据最终 CLI 和测试文件名确定。

## 7. 回滚与发布

- 迁移期间旧实现仅作为对照，不接受新功能。
- 新 Runtime 未达到 48/48 前不删除旧实现。
- Bundle 发布以 digest 为单位，失败时继续使用上一已验证 Bundle。
- 代码切换为一次性破坏性切换，不在主干长期并行维护双架构。
- 删除旧架构前保留独立提交点，便于定位迁移问题。
