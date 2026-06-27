# NL2SQL LangGraph 项目实现总结

## 当前实现状态

本项目是一个面向多租户 BI 场景的 NL2SQL agent 服务。核心链路由 LangGraph 编排，入口问题会依次经过初始化、记忆加载、上下文化改写、意图解析、指标检索、Schema 检索、SQL 生成、SQL 校验、可选执行、可选解释、记忆持久化和最终输出。

当前实现支持三类使用方式：

- CLI：支持单次问题，也支持不传问题时进入长对话 REPL。
- FastAPI：支持认证、无会话 `/api/nl2sql`、完整 conversation REST API。
- LangGraph Studio：`langgraph dev` 可直接加载 `graph/pipeline.py` 中的图定义。

## 运行时身份与认证

认证方案已经落地为“数据库用户表 + JWT access token + Refresh Token 轮换”。

核心文件：

- `api/auth.py`：认证配置、Argon2 密码哈希、JWT 生成与解码、Bearer token dependency。
- `api/auth_store.py`：认证 store 协议、PostgreSQL 实现、refresh token SHA-256 哈希存储、刷新轮换和撤销。
- `db/auth.sql`：`auth_users` 和 `auth_refresh_tokens` 表。
- `scripts/create_auth_user.py`：创建或更新认证用户。

主要规则：

- `JWT_SECRET_KEY` 必须配置。
- `AUTH_DATABASE_URL` 优先作为认证库 DSN；为空时回退到 `MEMORY_DATABASE_URL` 或 `MEMORY_POSTGRES_DSN`。
- access token 默认 30 分钟有效，refresh token 默认 7 天有效。
- refresh token 原文不入库，只保存 SHA-256 hash。
- `/api/auth/refresh` 会原子撤销旧 refresh token 并写入新 refresh token。
- `/api/auth/logout` 会撤销传入的 refresh token。
- 受保护业务接口以 JWT token identity 作为租户和用户身份来源。
- 请求体或查询参数里的 `tenant_id` / `user_id` 只用于迁移兼容：省略时使用 token identity；传入时必须匹配 token identity，否则返回 `403`。

## 数据库划分

项目当前有三类数据库职责：

- `DATABASE_URL`：业务查询数据库，用于 schema 检索、SQL 校验和 SQL 执行。
- `MEMORY_DATABASE_URL` 或 `MEMORY_POSTGRES_DSN`：会话记忆数据库，用于会话列表、历史消息、用户记忆和会话摘要。
- `AUTH_DATABASE_URL`：认证数据库，用于用户和 refresh token；未配置时回退到记忆库 DSN。

数据库脚本：

- `db/conversation_memory.sql`
  - `conversation_sessions`
  - `conversation_messages`
  - `user_memories`
  - `conversation_summaries`
- `db/auth.sql`
  - `auth_users`
  - `auth_refresh_tokens`

如果未配置记忆库 DSN，`create_conversation_store()` 会返回 `NullConversationStore`。这允许单轮 `/api/nl2sql` 继续运行，但会话 REST API 不具备真实持久化能力。认证 store 则要求存在可用 DSN，否则登录和 refresh token 管理无法工作。

## LangGraph 工作流

图定义在 `graph/pipeline.py`，节点实现集中在 `graph/node.py`。

当前节点：

1. `initialize`：标准化问题、租户、用户、会话 ID 和执行模式。
2. `load_memory`：读取会话历史和用户记忆。
3. `contextualize_question`：基于历史上下文改写当前问题。
4. `parse_intent`：使用 LLM 解析指标、维度、时间范围、过滤条件和目标表。
5. `search_metrics`：通过向量检索召回指标上下文。
6. `search_schema`：通过向量检索召回授权 schema 上下文。
7. `generate_sql`：基于意图、指标、schema 和历史上下文生成 PostgreSQL SQL。
8. `validate_sql`：限制单条 `SELECT`、授权表访问和最大返回行数。
9. `execute_sql`：在 `execute=true` 时执行 SQL。
10. `explain`：对查询结果生成自然语言解释。
11. `persist_memory`：有会话 ID 时保存用户消息与助手回答。
12. `finalize`：输出稳定响应结构。

路由逻辑在 `graph/router.py`：

- schema 检索失败会直接进入 `persist_memory -> finalize`。
- SQL 校验失败会在 `max_validation_attempts` 范围内回到 `generate_sql`。
- `execute=false` 或执行失败都会跳过解释，进入持久化和输出。

## SQL 安全策略

SQL 安全边界集中在生成、校验和执行阶段：

- 只允许 PostgreSQL 单条 `SELECT`。
- SQL 访问表必须来自 schema 检索返回的授权表集合。
- 自动补齐或限制 `LIMIT`，上限由请求参数 `max_limit` 控制。
- 执行阶段使用 statement timeout，默认请求超时参数为 `timeout_ms=10000`。
- schema 不足、生成失败或校验失败时不会执行 SQL。
- 业务查询数据库和记忆/认证数据库使用独立 DSN，避免误用业务库作为记忆库。

## FastAPI 接口

服务入口是 `api.app:app`，路由集中在 `api/routes.py`，请求响应 schema 在 `api/schemas.py`。

### 公开接口

- `GET /health`
- `POST /api/auth/login`
- `POST /api/auth/refresh`
- `POST /api/auth/logout`

### 受保护接口

- `GET /api/auth/me`
- `POST /api/nl2sql`
- `POST /api/conversations`
- `GET /api/conversations`
- `GET /api/conversations/{conversation_id}`
- `PATCH /api/conversations/{conversation_id}`
- `GET /api/conversations/{conversation_id}/messages`
- `POST /api/conversations/{conversation_id}/messages`

`/api/nl2sql` 是无会话 agent 调用入口。它验证 bearer access token 后解析租户身份，并调用 `graph.pipeline.run_nl2sql(...)`。

`/api/conversations/{conversation_id}/messages` 是多轮 agent 调用入口。它先校验会话归属，再传入 `conversation_id`、`user_id` 和记忆 store 调用 `run_nl2sql(...)`，使图可以读取历史消息并保存本轮消息。

## 请求和响应模型

主要请求模型：

- `LoginRequest`
- `RefreshRequest`
- `LogoutRequest`
- `Nl2SqlRequest`
- `ConversationCreateRequest`
- `ConversationUpdateRequest`
- `ConversationMessageRequest`

主要响应模型：

- `TokenResponse`
- `AuthUserResponse`
- `LogoutResponse`
- `Nl2SqlResponse`
- `ConversationResponse`
- `ConversationListResponse`
- `ConversationMessagesResponse`
- `ConversationNl2SqlResponse`

输入清洗策略：

- 必填文本字段会 trim 并拒绝空字符串。
- 可选 `tenant_id` / `user_id` 允许省略；如果传入空白字符串会拒绝。
- `timeout_ms`、`max_limit`、`max_validation_attempts`、`memory_history_limit` 都有范围限制。

## CLI

CLI 入口是 `main.py`。

单次问题：

```powershell
python main.py "按地区统计本月 GMV"
```

执行 SQL：

```powershell
python main.py "按地区统计本月 GMV" --tenant-id demo --execute
```

长对话 REPL：

```powershell
python main.py --tenant-id demo --user-id user-1
```

指定已有会话：

```powershell
python main.py --tenant-id demo --user-id user-1 --conversation-id conv-1
```

CLI 不走 HTTP JWT 认证，直接把租户、用户和会话参数传入 `run_nl2sql(...)`。

## RAG 与向量索引

RAG 相关实现包括：

- `rag/documents.py`：把指标和 schema catalog 转换为 embedding 文档。
- `rag/vector_store.py`：基于 PostgreSQL + pgvector 的 upsert 和语义检索。
- `graph/tools/sql_store.py`：检索指标和 schema，并按租户/授权表过滤。
- `scripts/rebuild_embeddings.py`：重建向量索引。

默认 embedding 配置：

- `EMBEDDING_MODEL=models/gemini-embedding-001`
- `EMBEDDING_DIM=768`

## Apifox / OpenAPI

当前可导入 Apifox 的 OpenAPI 文档：

```text
docs/apifox-openapi.json
```

生成脚本：

```powershell
conda run -n agents-env python scripts/export_apifox_openapi.py
```

该文档基于 FastAPI `app.openapi()` 生成，并补充：

- 中文接口分组和说明。
- 请求/响应示例。
- `BearerAuth` JWT 安全方案。
- 常见 `401`、`403`、`404`、`500` 响应说明。

## 测试覆盖

推荐回归命令：

```powershell
conda run -n agents-env python -m unittest discover -s tests -v
```

当前测试覆盖：

- API 健康检查。
- Auth token：密码哈希、claim 校验、签名、issuer/audience、过期和 token 类型。
- Auth store：用户 upsert、登录查找、refresh token 存储、轮换、过期、撤销和 hash 校验。
- Auth API：登录、刷新、me、logout 和 schema 校验。
- `/api/nl2sql`：鉴权、租户匹配、请求校验、异常响应。
- Conversation API：鉴权、创建、列表、详情隔离、更新、消息查询、多轮消息调用。
- LangGraph：状态、路由、上下文、节点、pipeline。
- SQL 工具：生成、校验、执行、解释和检索。
- RAG 文档、向量 store、memory store、LLM/Embedding adapter 和 CLI。

最近一次完整验证结果：`conda run -n agents-env python -m unittest discover -s tests -v` 通过 145 个测试。

## 当前边界

- 认证已经具备登录、access token、refresh token 轮换和登出撤销，但尚未实现细粒度角色授权；`roles` 已进入 token 和用户表，后续可用于 RBAC。
- refresh token 会记录 hash、过期、撤销和替换关系，但当前路由未记录 user agent / client IP。
- 会话删除接口尚未实现，当前支持归档字段 `archived`。
- `conversation_summaries` 表已预留，但当前上下文加载主要依赖最近消息和用户记忆。
- OpenAPI 文档由脚本生成；路由或 schema 变更后需要重新执行 `scripts/export_apifox_openapi.py`。
