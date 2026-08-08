# NL2SQL / Data Agent 项目概况

## 当前状态

项目已完成 `plan.md` 中第 0–8 阶段。当前分支为
`Agent`，最新提交为 `3027cb2`。系统默认不部署任何业务数据，用户必须自行
上传或注册数据源并激活语义绑定；OList 仅保留为显式兼容和离线回归资产。

## 核心架构

- 默认上传运行时负责按 tenant/user 隔离的会话，以及无数据源时的安全失败；
  API 数据集查询服务负责用户数据源的规划与执行。
- 用户请求经结构化 Planner、Query IR、语义绑定、方言编译和只读执行，不允许
  模型直接生成并执行物理 SQL。
- 可选 Domain Pack/Enterprise Binding 运行时继续用于显式兼容部署，不参与
  默认应用启动。
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
- 前端已接入数据源上传/注册、语义绑定、SSE 消费和运行取消；没有激活绑定时
  会打开数据源面板并阻止发送问题。
- API、CLI、Studio 和公共请求契约默认使用 `user-dataset` / `dataset`，不会
  回退到 OList。
- 可通过 `DATA_AGENT_BUNDLE_PATHS_FILE` 加载其他已验证的 Commerce 企业
  bundle；OList 只能通过显式兼容构建器启动。

## 控制面与安全边界

- 认证使用 `AUTH_DATABASE_URL` 指向的 PostgreSQL；用户数据源注册、绑定、
  文件快照、会话和运行事件位于独立控制面状态目录。
- 默认启动不读取业务 `DATABASE_URL`，也不加载 OList bundle 或创建其数据库
  连接。
- PostgreSQL 密码不进入注册数据，只保存 `credential_ref`，运行时从部署环境
  解析。
- 查询强制只读、关系白名单、行数和时间预算、版本校验及安全错误输出。
- 数据源、会话、运行事件和记忆操作均以认证主体的 tenant/user 权限为边界。

## 验证状态

- 后端：435 项测试通过。
- 前端：45 项测试通过，TypeScript 和 Vite 生产构建通过。
- OpenAPI/Apifox 契约、前端 AgentResponse schema、wheel 构建和离线 OList
  黄金用例均纳入发布门禁。

## 后续改进重点

- 为记忆提案补充完整的前端审批界面。
- 为运行事件增加保留期、清理任务，并在多实例部署时迁移到共享事件存储和
  分布式取消协调。
- 扩展用户数据源的多关系语义建模与关联查询能力。
- 将通用部署从 Commerce 企业 binding 进一步扩展为可选择不同 Domain Pack
  和执行图。
