# NL2SQL LangGraph

面向多租户 BI 场景的安全 NL2SQL 工作流。项目使用 LangChain 统一 LLM 与 Embeddings 接口，使用 LangGraph 编排自然语言到 SQL 的 agent 工作流，并通过独立数据库支持 JWT 登录态、Refresh Token 轮换和多轮会话记忆。

## 当前能力

- 单轮 NL2SQL：`POST /api/nl2sql` 调用 LangGraph agent pipeline，把自然语言问题转换为 SQL，可选择只校验或实际执行查询。
- 多轮会话：基于 `tenant_id + user_id + conversation_id` 读取历史消息和用户记忆，支持上下文化改写后继续生成 SQL。
- 登录态管理：数据库用户表 + JWT access token + Refresh Token 轮换，受保护 API 统一使用 token identity。
- REST API：提供认证、单轮 NL2SQL、会话管理和会话消息接口。
- CLI：支持单次问题，也支持不传问题时进入长对话 REPL。
- Apifox 文档：`docs/apifox-openapi.json` 可直接导入 Apifox。
- 数据库隔离：业务查询数据库、会话记忆数据库、认证数据库可分开配置；认证库未单独配置时回退到记忆库 DSN。

## 工作流

```text
question
  -> initialize
  -> load_memory
  -> contextualize_question
  -> parse_intent
  -> search_metrics
  -> search_schema
  -> generate_sql
  -> validate_sql
       -> validation failed -> generate_sql
       -> execute=false -> persist_memory -> finalize
       -> execute=true -> execute_sql -> explain -> persist_memory -> finalize
```

SQL 执行前必须满足：

- Schema 检索返回非空授权表集合。
- SQL 仅包含单条 PostgreSQL `SELECT`。
- SQL 只访问授权表。
- 查询自动限制最大返回行数。
- 数据库执行使用 statement timeout。

## 项目结构

```text
api/        FastAPI 应用、认证、路由和请求响应 schema
graph/      LangGraph 状态、节点、路由、pipeline 和会话记忆 store
graph/tools SQL 生成、校验、执行、解释、上下文化改写和语义检索工具
core/       环境加载、LLM 和 Embeddings 适配
engine/     意图解析模型与解析器
rag/        指标和 schema 文档构造、pgvector 检索
catalog/    本地 schema/metric catalog 加载
db/         PostgreSQL 初始化脚本
scripts/    向量重建、认证用户创建、Apifox OpenAPI 导出脚本
tests/      unittest 测试
```

## 环境

项目开发和验证使用 Conda 环境：

```powershell
conda activate agents-env
pip install -e ".[dev]"
```

复制 `.env.example` 为 `.env`，配置模型、数据库和 JWT。默认使用 Google Gemini；设置 `LLM_PROVIDER=deepseek` 可使用 DeepSeek 的 OpenAI 兼容接口。

关键配置：

```env
LLM_PROVIDER=google
DEFAULT_MODEL_NAME=gemini-2.5-flash
GEMINI_API_KEY=

# DeepSeek alternative
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com

EMBEDDING_MODEL=models/gemini-embedding-001
EMBEDDING_DIM=768

# 业务查询数据库：schema 检索、SQL 校验、SQL 执行
DATABASE_URL=postgresql://user:password@localhost:5432/nl2sql_data

# 会话记忆数据库：会话列表、历史消息、用户记忆
MEMORY_DATABASE_URL=postgresql://user:password@localhost:5432/nl2sql_memory

# 认证数据库：为空时回退到 MEMORY_DATABASE_URL
AUTH_DATABASE_URL=
JWT_SECRET_KEY=
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
JWT_ISSUER=nl2sql-api
JWT_AUDIENCE=nl2sql-client
```

## 数据库初始化

会话 REST API 需要初始化记忆库：

```powershell
psql "$env:MEMORY_DATABASE_URL" -f db/conversation_memory.sql
```

认证登录需要初始化认证库。`AUTH_DATABASE_URL` 不为空时使用它；否则使用 `MEMORY_DATABASE_URL`：

```powershell
$authDsn = if ($env:AUTH_DATABASE_URL) { $env:AUTH_DATABASE_URL } else { $env:MEMORY_DATABASE_URL }
psql $authDsn -f db/auth.sql
```

创建或更新一个登录用户：

```powershell
python scripts/create_auth_user.py `
  --tenant-id demo `
  --user-id user-1 `
  --username alice `
  --password "secret" `
  --roles user
```

说明：

- `db/conversation_memory.sql` 和 `db/auth.sql` 是运行数据库的初始化脚本，不需要每次启动 API 都执行。
- 本机只跑单元测试时不需要初始化真实数据库。
- 本机真实启动 API 并走登录/会话调试时，需要先初始化对应表并创建至少一个用户。
- 如果不配置 `MEMORY_DATABASE_URL`，系统会使用空记忆实现；单轮 `/api/nl2sql` 仍可运行，但会话 REST API 不具备持久化能力。

## CLI

只生成并校验 SQL：

```powershell
python main.py "按地区统计本月 GMV"
```

执行 SQL 并生成解释：

```powershell
python main.py "按地区统计本月 GMV" --execute --tenant-id demo
```

进入长对话 REPL：

```powershell
python main.py --tenant-id demo --user-id user-1
```

复用已有会话：

```powershell
python main.py --tenant-id demo --user-id user-1 --conversation-id conv-1
```

在 REPL 中输入 `exit` 或 `quit` 退出。

## LangGraph Studio

安装开发依赖后运行：

```powershell
langgraph dev
```

图定义位于 `graph/pipeline.py`。运行时模型、Embeddings、业务数据库连接和记忆数据库连接通过 `GraphContext` 注入，不写入持久化图状态。

## FastAPI 后端

启动 HTTP API：

```powershell
uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

### 认证流程

登录并保存返回的 `access_token` 和 `refresh_token`：

```powershell
curl -X POST http://127.0.0.1:8000/api/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"tenant_id\":\"demo\",\"username\":\"alice\",\"password\":\"secret\"}"
```

刷新 token。刷新成功后旧 refresh token 会被撤销，新响应会返回新的 access token 和 refresh token：

```powershell
curl -X POST http://127.0.0.1:8000/api/auth/refresh `
  -H "Content-Type: application/json" `
  -d "{\"refresh_token\":\"<refresh_token>\"}"
```

读取当前 token 身份：

```powershell
curl http://127.0.0.1:8000/api/auth/me `
  -H "Authorization: Bearer <access_token>"
```

退出当前 refresh token：

```powershell
curl -X POST http://127.0.0.1:8000/api/auth/logout `
  -H "Content-Type: application/json" `
  -d "{\"refresh_token\":\"<refresh_token>\"}"
```

受保护接口必须携带：

```http
Authorization: Bearer <access_token>
```

`tenant_id` 和 `user_id` 请求字段仅用于迁移兼容：省略时使用 token identity；传入时必须与 token identity 一致，否则返回 `403`。

### 单轮 NL2SQL

`/api/nl2sql` 是无会话的 agent 调用入口，会直接调用 `graph.pipeline.run_nl2sql(...)`。

```powershell
curl -X POST http://127.0.0.1:8000/api/nl2sql `
  -H "Authorization: Bearer <access_token>" `
  -H "Content-Type: application/json" `
  -d "{\"question\":\"按地区统计本月 GMV\",\"execute\":false}"
```

可选传入 `tenant_id`，但必须等于 token 中的租户：

```json
{
  "question": "按地区统计本月 GMV",
  "tenant_id": "demo",
  "execute": false,
  "timeout_ms": 10000,
  "max_limit": 1000,
  "max_validation_attempts": 2
}
```

`execute=false` 只返回意图、SQL 和 trace；`execute=true` 会在 SQL 校验通过后执行查询并生成解释。

### Conversation REST API

创建会话：

```powershell
curl -X POST http://127.0.0.1:8000/api/conversations `
  -H "Authorization: Bearer <access_token>" `
  -H "Content-Type: application/json" `
  -d "{\"title\":\"GMV 分析\"}"
```

查询当前用户会话列表：

```powershell
curl "http://127.0.0.1:8000/api/conversations?limit=20&include_archived=false" `
  -H "Authorization: Bearer <access_token>"
```

查询会话详情：

```powershell
curl "http://127.0.0.1:8000/api/conversations/{conversation_id}" `
  -H "Authorization: Bearer <access_token>"
```

更新标题或归档状态：

```powershell
curl -X PATCH http://127.0.0.1:8000/api/conversations/{conversation_id} `
  -H "Authorization: Bearer <access_token>" `
  -H "Content-Type: application/json" `
  -d "{\"title\":\"本月 GMV 追踪\",\"archived\":false}"
```

查询会话消息：

```powershell
curl "http://127.0.0.1:8000/api/conversations/{conversation_id}/messages?limit=50" `
  -H "Authorization: Bearer <access_token>"
```

发送多轮消息并执行 NL2SQL：

```powershell
curl -X POST http://127.0.0.1:8000/api/conversations/{conversation_id}/messages `
  -H "Authorization: Bearer <access_token>" `
  -H "Content-Type: application/json" `
  -d "{\"question\":\"那华东地区呢？\",\"execute\":false,\"memory_history_limit\":8}"
```

多轮消息接口会先校验会话是否属于 token identity，再读取历史上下文生成 `contextualized_question`，最后将本轮用户消息和助手回答写入记忆库。

## API 接口速览

| Method | Path | 鉴权 | 作用 |
| --- | --- | --- | --- |
| `GET` | `/health` | 否 | 健康检查 |
| `POST` | `/api/auth/login` | 否 | 用户名密码登录 |
| `POST` | `/api/auth/refresh` | 否 | 使用 refresh token 轮换 token |
| `GET` | `/api/auth/me` | 是 | 获取当前 access token 身份 |
| `POST` | `/api/auth/logout` | 否 | 撤销指定 refresh token |
| `POST` | `/api/nl2sql` | 是 | 无会话单轮 NL2SQL agent 调用 |
| `POST` | `/api/conversations` | 是 | 创建会话 |
| `GET` | `/api/conversations` | 是 | 查询用户会话列表 |
| `GET` | `/api/conversations/{conversation_id}` | 是 | 查询会话详情 |
| `PATCH` | `/api/conversations/{conversation_id}` | 是 | 更新会话标题或归档状态 |
| `GET` | `/api/conversations/{conversation_id}/messages` | 是 | 查询会话消息历史 |
| `POST` | `/api/conversations/{conversation_id}/messages` | 是 | 在指定会话内发送问题并执行 NL2SQL |

## Apifox 文档

当前 API 的 OpenAPI 文档位于：

```text
docs/apifox-openapi.json
```

Apifox 导入方式：选择“导入数据” -> “OpenAPI/Swagger” -> 导入 `docs/apifox-openapi.json`。

路由变更后可重新导出：

```powershell
conda run -n agents-env python scripts/export_apifox_openapi.py
```

## 测试

```powershell
conda run -n agents-env python -m unittest discover -s tests -v
```

当前测试覆盖：

- Auth/JWT：密码哈希、token claim 校验、refresh token 轮换、登出撤销。
- Auth API：登录、刷新、me、logout 以及 schema 校验。
- 单轮 `/api/nl2sql`：鉴权、租户匹配、参数校验、异常响应。
- Conversation REST API：鉴权、创建、列表、详情隔离、更新、消息查询、多轮消息。
- LangGraph：状态、路由、上下文、节点、pipeline、SQL 工具、RAG 文档、memory store 和 CLI。

更多当前实现说明见 `summary.md`。
