# NL2SQL / Data Agent 项目概况

## 当前状态

项目已完成 `plan.md` 中第 0–7 阶段，当前分支为 `Agent`，最新功能提交为
`5ccdf08`。系统已从固定 OList NL2SQL 演进为具备治理能力的多数据源分析
Agent，同时保留 OList Commerce 作为默认部署。

## 核心架构

- CLI、FastAPI、Web 前端和 LangGraph Studio 统一调用公共
  `DataAgentRuntime`。
- 请求经结构化 Planner、Logical Query Plan、策略校验、方言编译和只读执行，
  不允许模型直接生成并执行物理 SQL。
- Domain Pack 管理语义、指标、词汇和策略；Enterprise Binding 管理物理表、
  字段、关联、访问范围及凭据引用。
- GPT/OpenAI、Qwen、Gemini、Claude、GLM、DeepSeek 及 OpenAI-compatible
  Provider 共用统一模型接口和结构化输出契约。

## 已实现能力

- 支持 PostgreSQL 注册，以及 SQLite、CSV、XLSX 文件上传和隔离快照。
- 数据源目录发现、版本化注册、语义字段映射、人工确认激活和会话版本钉住。
- 用户自选数据源通过统一 Planner、Query IR 和只读连接器完成查询。
- 支持 `plan`、`preview`、`execute` 三种模式，以及答案、表格和字段受限的
  安全柱状图。
- SSE 流式事件、按租户/用户隔离的运行取消、SQLite 持久化事件回放。
- 记忆提案查询和批准/拒绝 API；用户记忆按所有者隔离，企业和事件记忆要求
  管理员权限。
- 前端已接入数据源选择、语义绑定、SSE 消费和运行取消。
- 可通过 `DATA_AGENT_BUNDLE_PATHS_FILE` 加载其他已验证的 Commerce 企业
  bundle；默认 OList 入口保持兼容。

## 控制面与安全边界

- 认证、会话、记忆和默认业务数据使用 PostgreSQL；用户数据源注册、绑定、
  文件快照和运行事件位于独立控制面状态目录。
- PostgreSQL 密码不进入注册数据，只保存 `credential_ref`，运行时从部署环境
  解析。
- 查询强制只读、关系白名单、行数和时间预算、版本校验及安全错误输出。
- 数据源、会话、运行事件和记忆操作均以认证主体的 tenant/user 权限为边界。

## 验证状态

- 后端：430 项测试及 609 个子测试通过。
- 前端：46 项测试通过，TypeScript 和 Vite 生产构建通过。
- OpenAPI/Apifox 契约、前端 AgentResponse schema、wheel 构建和离线 OList
  黄金用例均纳入发布门禁。

## 后续改进重点

- 为记忆提案补充完整的前端审批界面。
- 为运行事件增加保留期、清理任务，并在多实例部署时迁移到共享事件存储和
  分布式取消协调。
- 扩展用户数据源的多关系语义建模与关联查询能力。
- 将通用部署从 Commerce 企业 binding 进一步扩展为可选择不同 Domain Pack
  和执行图。
