# NL2SQL LangGraph

面向多租户 BI 场景的安全 NL2SQL 工作流。项目使用 LangChain 统一模型与 Embeddings 接口，使用 LangGraph 显式编排问答链路，并通过独立记忆数据库支持多轮对话。

## 当前能力

- 单轮 NL2SQL：自然语言问题生成 SQL，可选择只校验或执行查询。
- 多轮对话：基于 `tenant_id + user_id + conversation_id` 读取历史消息和用户记忆。
- REST API：提供无会话 `/api/nl2sql` 和完整 conversation REST API。
- CLI：支持单次问题，也支持不传问题时进入长对话 REPL。
- 数据库隔离：业务查询数据库和会话记忆数据库使用不同连接串。

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

## 环境

项目开发环境使用 Conda：

```powershell
conda activate agents-env
pip install -e ".[dev]"
```

复制 `.env.example` 为 `.env`，配置模型和数据库连接。默认使用 Google Gemini；设置 `LLM_PROVIDER=deepseek` 可使用 DeepSeek 的 OpenAI 兼容接口。

关键数据库配置：

```env
# 业务查询数据库：Schema 检索、SQL 校验、SQL 执行
DATABASE_URL=postgresql://user:password@localhost:5432/nl2sql_data

# 记忆数据库：会话列表、历史消息、用户记忆
MEMORY_DATABASE_URL=postgresql://user:password@localhost:5432/nl2sql_memory
```

初始化记忆库：

```powershell
psql "$env:MEMORY_DATABASE_URL" -f db/conversation_memory.sql
```

如果未配置 `MEMORY_DATABASE_URL`，系统会使用空记忆实现。单轮 `/api/nl2sql` 仍可运行，但 conversation REST API 不会具备持久化能力。

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

### 单轮 NL2SQL

```powershell
curl -X POST http://127.0.0.1:8000/api/nl2sql `
  -H "Content-Type: application/json" `
  -d "{\"question\":\"按地区统计本月 GMV\",\"tenant_id\":\"demo\",\"execute\":false}"
```

`execute=false` 只返回意图、SQL 和 trace；`execute=true` 会在 SQL 校验通过后执行查询并生成解释。

### Conversation REST API

创建会话：

```powershell
curl -X POST http://127.0.0.1:8000/api/conversations `
  -H "Content-Type: application/json" `
  -d "{\"tenant_id\":\"demo\",\"user_id\":\"user-1\",\"title\":\"GMV 分析\"}"
```

查询当前用户会话列表：

```powershell
curl "http://127.0.0.1:8000/api/conversations?tenant_id=demo&user_id=user-1&limit=20&include_archived=false"
```

查询会话详情：

```powershell
curl "http://127.0.0.1:8000/api/conversations/{conversation_id}?tenant_id=demo&user_id=user-1"
```

更新标题或归档状态：

```powershell
curl -X PATCH http://127.0.0.1:8000/api/conversations/{conversation_id} `
  -H "Content-Type: application/json" `
  -d "{\"tenant_id\":\"demo\",\"user_id\":\"user-1\",\"title\":\"本月 GMV 追踪\",\"archived\":false}"
```

查询会话消息：

```powershell
curl "http://127.0.0.1:8000/api/conversations/{conversation_id}/messages?tenant_id=demo&user_id=user-1&limit=50"
```

发送多轮消息：

```powershell
curl -X POST http://127.0.0.1:8000/api/conversations/{conversation_id}/messages `
  -H "Content-Type: application/json" `
  -d "{\"tenant_id\":\"demo\",\"user_id\":\"user-1\",\"question\":\"那华东地区呢？\",\"execute\":false,\"memory_history_limit\":8}"
```

多轮消息接口会先校验会话是否属于该 `tenant_id + user_id`，再读取历史上下文生成 `contextualized_question`，最后将本轮用户消息和助手回答写入记忆库。

## API 接口速览

| Method | Path | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/nl2sql` | 无会话单轮 NL2SQL |
| `POST` | `/api/conversations` | 创建会话 |
| `GET` | `/api/conversations` | 查询用户会话列表 |
| `GET` | `/api/conversations/{conversation_id}` | 查询会话详情 |
| `PATCH` | `/api/conversations/{conversation_id}` | 更新会话标题或归档状态 |
| `GET` | `/api/conversations/{conversation_id}/messages` | 查询会话消息历史 |
| `POST` | `/api/conversations/{conversation_id}/messages` | 在指定会话内发送问题并执行 NL2SQL |

当前第一版 API 没有内置鉴权中间件。调用方需要可信地传入 `tenant_id` 和 `user_id`，接口内部使用这两个字段做会话隔离。

## 测试

```powershell
python -m unittest discover -s tests -v
```

Conda 环境下可使用：

```powershell
conda run -n agents-env python -m unittest discover -s tests -v
```

针对 conversation API 的测试位于 `tests/test_api_conversations.py`，覆盖创建会话、用户隔离、更新会话、查询消息和发送多轮消息。

更多当前实现说明见 `summary.md`。
