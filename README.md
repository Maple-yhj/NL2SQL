# NL2SQL LangGraph

面向多租户 BI 场景的安全 NL2SQL 工作流，使用 LangChain 统一模型与 Embeddings，使用 LangGraph 显式编排执行过程。

## 工作流

```text
question
  -> parse_intent
  -> search_metrics
  -> search_schema
  -> generate_sql
  -> validate_sql
       -> validation failed -> generate_sql
       -> execute=false -> finalize
       -> execute=true -> execute_sql -> explain -> finalize
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

## CLI

只生成并校验 SQL：

```powershell
python main.py "按地区统计本月 GMV"
```

执行 SQL 并生成解释：

```powershell
python main.py "按地区统计本月 GMV" --execute --tenant-id demo
```

## LangGraph Studio

安装开发依赖后运行：

```powershell
langgraph dev
```

图定义位于 `graph/pipeline.py`，运行时模型、Embeddings 和数据库参数通过 `GraphContext` 注入，不写入持久化状态。

## 测试

```powershell
python -m unittest discover -s tests -v
```
