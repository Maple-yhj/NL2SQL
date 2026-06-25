# NL2SQL LangGraph 项目实现总结

## 当前实现状态

本项目是一个面向多租户 BI 场景的 NL2SQL 工作流。核心链路由 LangGraph 编排，入口问题会依次经过初始化、记忆加载、上下文化改写、意图解析、指标检索、Schema 检索、SQL 生成、SQL 校验、可选执行、可选解释、记忆持久化和最终输出。

当前实现已经支持两类使用方式：

- CLI：支持单次问答，也支持不传问题时进入长对话 REPL。
- FastAPI：支持无会话的 `/api/nl2sql`，也支持完整 conversation REST API。

SQL 安全策略集中在校验和执行阶段，主要包括：

- 只允许单条 PostgreSQL `SELECT`。
- 查询表必须来自授权 Schema 检索结果。
- 自动限制最大返回行数。
- 执行 SQL 时设置 statement timeout。
- 生成失败、Schema 不足或校验失败时不会直接执行 SQL。

## 数据库划分

项目当前将业务查询数据库和记忆数据库分开配置：

- `DATABASE_URL`：业务数据查询库，用于 Schema 检索、SQL 校验和 SQL 执行。
- `MEMORY_DATABASE_URL`：会话记忆库，用于保存会话列表、历史消息、用户记忆和会话摘要。

记忆库建表脚本位于 `db/conversation_memory.sql`，包含：

- `conversation_sessions`：会话元数据，包含 `tenant_id`、`conversation_id`、`user_id`、`title`、`archived`、创建时间和更新时间。
- `conversation_messages`：用户与助手消息历史。
- `user_memories`：按用户维度保存的长期偏好或历史信息。
- `conversation_summaries`：预留的会话摘要表。

如果未配置 `MEMORY_DATABASE_URL`，系统会退化到空记忆实现，普通 `/api/nl2sql` 仍可使用，但 conversation REST API 不具备持久化能力。

## 多轮对话设计

多轮对话依赖三个身份字段：

- `tenant_id`：租户隔离。
- `user_id`：用户隔离。
- `conversation_id`：会话隔离。

每轮会话请求进入图后：

1. `initialize` 标准化输入问题、租户、用户和会话 ID。
2. `load_memory` 从记忆库读取最近的会话历史和用户记忆。
3. `contextualize_question` 使用历史上下文改写当前问题，补全省略条件。
4. 后续 NL2SQL 节点基于改写后的问题生成和校验 SQL。
5. `persist_memory` 将本轮用户问题和助手回答写入记忆库。

当前用户隔离规则是 `tenant_id + user_id + conversation_id`。接口在读取详情、更新会话、查询消息、发送消息前都会校验会话是否属于该用户；不属于时返回 `404`。

## FastAPI 接口

服务入口为 `api.app:app`。

启动命令：

```powershell
uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

### 健康检查

`GET /health`

返回服务存活状态。

### 单轮 NL2SQL

`POST /api/nl2sql`

适合无会话的单次问答。

请求体：

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

返回字段包括：

- `ok`
- `question`
- `tenant_id`
- `intent`
- `sql`
- `rows`
- `answer`
- `error`
- `trace`

### 创建会话

`POST /api/conversations`

请求体：

```json
{
  "tenant_id": "demo",
  "user_id": "user-1",
  "title": "GMV 分析"
}
```

返回会话元数据：

```json
{
  "tenant_id": "demo",
  "conversation_id": "generated-id",
  "user_id": "user-1",
  "title": "GMV 分析",
  "archived": false,
  "created_at": "2026-06-25T00:00:00Z",
  "updated_at": "2026-06-25T00:00:00Z"
}
```

### 查询会话列表

`GET /api/conversations?tenant_id=demo&user_id=user-1&limit=20&include_archived=false`

返回当前用户的会话列表，默认不包含归档会话。

### 查询会话详情

`GET /api/conversations/{conversation_id}?tenant_id=demo&user_id=user-1`

只返回属于该 `tenant_id + user_id` 的会话；否则返回 `404`。

### 更新会话

`PATCH /api/conversations/{conversation_id}`

可更新会话标题或归档状态。

请求体：

```json
{
  "tenant_id": "demo",
  "user_id": "user-1",
  "title": "本月 GMV 追踪",
  "archived": false
}
```

### 查询会话消息

`GET /api/conversations/{conversation_id}/messages?tenant_id=demo&user_id=user-1&limit=50`

返回该会话内最近的用户与助手消息。

### 发送会话消息

`POST /api/conversations/{conversation_id}/messages`

这是多轮对话的主要问答接口，会读取历史上下文并在完成后保存本轮消息。

请求体：

```json
{
  "tenant_id": "demo",
  "user_id": "user-1",
  "question": "那华东地区呢？",
  "execute": false,
  "timeout_ms": 10000,
  "max_limit": 1000,
  "max_validation_attempts": 2,
  "memory_history_limit": 8
}
```

返回字段在 `/api/nl2sql` 基础上增加：

- `contextualized_question`：结合历史上下文后的问题。
- `conversation_id`
- `user_id`

## CLI 长对话

不传问题时进入 REPL：

```powershell
python main.py --tenant-id demo --user-id user-1
```

指定已有会话：

```powershell
python main.py --tenant-id demo --user-id user-1 --conversation-id conv-1
```

带执行模式：

```powershell
python main.py --tenant-id demo --user-id user-1 --conversation-id conv-1 --execute
```

## 测试覆盖

当前已有测试覆盖：

- API 健康检查。
- 单轮 `/api/nl2sql`。
- conversation REST API 的创建、列表、详情隔离、更新、消息查询和发送消息。
- LangGraph 状态、路由、上下文、节点和 pipeline。
- 记忆存储、SQL 生成、SQL 执行工具、RAG 文档和 CLI。

推荐回归命令：

```powershell
conda run -n agents-env python -m unittest discover -s tests -v
```

本次 conversation API 开发中已经验证过核心测试集合，69 个相关测试通过。

## 当前边界

当前第一版 REST API 只实现会话管理和多轮问答，没有实现鉴权中间件。调用方需要可信地传入 `tenant_id` 和 `user_id`。如果要面向真实外部用户开放，下一步应增加认证、授权、分页游标、删除或软删除策略，以及更完整的错误码规范。
