# P2 剩余工作计划

## 当前状态

当前 P2 已经有一个固定顺序的 agent pipeline，但还不是完整的 ReAct Tool Use Agent。

已经完成：

- `agent/tools/sql_store.py` 中已有 `search_metrics` 和 `search_schema`
- `agent/tools/validate_sql.py` 中已有 `validate_sql`
- `agent/sql_generator.py` 已经基于 metric context 和 schema context 生成 SQL
- `agent/pipeline.py` 已经形成固定流程：
  - `search_metrics`
  - `search_schema`
  - `parse_intent`
  - `generate_sql`
  - `validate_sql`
  - 可选执行 SQL
- `agent/pipeline.py` 已经支持 SQL 校验失败后重试一次
- 已经补充 agent tools、SQL generator、pipeline retry 相关单元测试

当前验证命令：

```powershell
D:\Env\miniconda3\envs\agents-env\python.exe -m unittest discover -s tests
```

当前预期结果：

```text
29 tests OK
```

## P2 尚未完成的目标

1. 还没有真正的 ReAct 主循环。

   当前行为是确定性的固定 pipeline。模型还不能动态选择工具、观察工具结果，再决定下一步动作。

2. P2 工具集还不完整。

   文档规划中仍未实现的工具：

   - `agent/tools/execute_sql.py`
   - `agent/tools/explain_result.py`
   - 后续可选：`agent/tools/get_table_sample.py`

3. schema 检索还可以进一步收敛。

   `search_schema()` 已经支持 `table_names` 参数，但 `agent/pipeline.py` 还没有利用 metric 的 `base_table` / `join_tables` 去限制 schema 检索范围。

4. SQL 执行仍然耦合 P1 engine。

   `agent/pipeline.py` 目前仍然直接导入 `engine.executor.execute_readonly_sql`。P2 阶段应该把 SQL 执行封装为 agent tool。

5. 查询结果还没有业务解释层。

   当前 CLI 主要打印 SQL 和 rows。P2 阶段应该基于 `question`、`sql`、`rows`、`metrics_result` 输出一段面向业务用户的中文解释。

6. 集成测试还不够完整。

   当前测试主要是单元测试和 mock 测试。后续需要补几组更高层的 pipeline 测试，覆盖正常成功、重试成功、重试失败、无 schema 等场景。

## 推荐实现顺序

### 阶段 1：先稳定固定 pipeline

目标：在引入 ReAct 之前，先把当前确定性 P2 pipeline 做稳。

任务：

1. 在 `agent/pipeline.py` 中提取 metric 表线索：

   - `base_table`
   - `join_tables`

2. 将这些表线索传给 `search_schema(..., table_names=...)`。

3. 增加测试，证明 `search_schema()` 收到了预期的 `table_names`。

4. 给 `search_metrics()` 补基础参数校验：

   - 拒绝空 `query`
   - 拒绝空 `tenant_id`
   - 限制 `top_k` 在安全范围内

完成标准：

- [x] 所有现有测试继续通过。
- [x] 新测试能证明 `base_table` / `join_tables` 已用于限制 schema 检索。
- [x] `search_metrics()` 和 `search_schema()` 的基础参数校验能力基本一致。

### 阶段 2：新增 agent 执行工具

目标：把 SQL 执行放到 P2 工具边界之后，不再让 pipeline 直接调用 P1 executor。

新增文件：

```text
agent/tools/execute_sql.py
```

建议 API：

```python
async def execute_sql(
    sql: str,
    tenant_id: str,
    *,
    dsn: str | None = None,
    timeout_ms: int = 10_000,
) -> dict:
    ...
```

建议成功返回结构：

```python
{
    "ok": True,
    "sql": "...",
    "tenant_id": "demo",
    "rows": [...],
    "row_count": 10,
    "message": "success",
}
```

建议失败返回结构：

```python
{
    "ok": False,
    "sql": "...",
    "tenant_id": "demo",
    "rows": [],
    "row_count": 0,
    "message": "...",
}
```

规则：

- 只执行已经校验通过的 normalized SQL。
- 保留只读保护。
- 保留 statement timeout。
- 不要静默吞掉数据库异常，要么返回清晰失败对象，要么保持一致地抛出异常。

完成标准：

- [x] `agent/tools/__init__.py` 导出 `execute_sql`。
- [x] 使用 mock DB execution 补充成功和失败路径测试。
- [x] `agent/pipeline.py` 不再直接导入 `engine.executor.execute_readonly_sql`。

### 阶段 3：新增结果解释工具

目标：让 P2 输出业务解释，而不仅仅是 SQL 和原始 rows。

新增文件：

```text
agent/tools/explain_result.py
```

建议 API：

```python
async def explain_result(
    *,
    question: str,
    sql: str,
    rows: list[dict],
    metrics_result: dict,
    llm=None,
) -> dict:
    ...
```

建议返回结构：

```python
{
    "ok": True,
    "explanation": "...",
    "message": "success",
}
```

初始版本可以先做规则版：

- 如果没有 rows：说明未返回数据
- 如果只有一行：概括该行 key-value
- 如果有多行：说明返回行数和主要字段

LLM 解释可以后续再引入。

完成标准：

- [ ] `agent/tools/__init__.py` 导出 `explain_result`。
- [ ] pipeline 返回结果中增加 `"explanation"`。
- [ ] CLI 的 `--agent` 模式可以打印 explanation。
- [ ] 单元测试覆盖空结果、单行结果、多行结果。

### 阶段 4：将 pipeline 接入工具化执行和解释

目标：让固定 P2 pipeline 变成工具化 pipeline。

任务：

1. 在 `agent/pipeline.py` 中用 `agent.tools.execute_sql.execute_sql` 替换直接执行。
2. 如果 `execute=True` 且执行成功，调用 `explain_result`。
3. 可选增加 `tool_trace`，记录每个工具调用情况。

建议 `tool_trace` item：

```python
{
    "tool": "validate_sql",
    "ok": True,
    "summary": "success",
}
```

完成标准：

- [ ] `run_agent_nl2sql(..., execute=True)` 返回：
  - `rows`
  - `executed_sql`
  - `explanation`
  - 可选 `tool_trace`
- [ ] 所有现有测试继续通过。
- [ ] 新测试覆盖执行成功和执行失败。

### 阶段 5：实现最小 ReAct loop

目标：在固定 pipeline 稳定后，引入可控的动态工具调用。

新增文件：

```text
agent/react_loop.py
```

最小工具集：

- `search_metrics`
- `search_schema`
- `generate_sql`
- `validate_sql`
- `execute_sql`
- `explain_result`

推荐 state 结构：

```python
{
    "question": "...",
    "tenant_id": "demo",
    "metrics_result": {},
    "schema_result": {},
    "intent": {},
    "sql": "",
    "validation": {},
    "rows": [],
    "explanation": "",
    "tool_trace": [],
}
```

推荐约束：

- `max_steps=6`
- 只允许调用白名单工具
- 每次工具调用必须产生 observation
- 当 SQL 校验通过且 explanation 准备好后停止
- 超过最大步数后返回失败

完成标准：

- [ ] ReAct loop 能完成固定 pipeline 的 happy path。
- [ ] 测试至少覆盖：
  - 正常成功
  - 校验失败后重试
  - 超过最大步数失败

## 暂缓处理的工作

这些工作有价值，但不建议阻塞当前 P2.1。

1. `get_table_sample.py`

   暂缓到 ReAct loop 实现后再做。当前固定 pipeline 中，`search_schema.sample_values` 已经够用。动态样本工具适合在模型能自主判断“需要看真实样本行”时再引入。

2. 强 tenant guard

   当前向量检索已经使用 `tenant_id`，但生成 SQL 还没有强制注入 tenant 过滤。这个更接近 P3/security，除非当前阶段要执行真实多租户数据。

3. Prompt injection 检测

   更适合放到 security 阶段统一处理。

4. 完整 eval 测评体系

   等固定 pipeline 和 ReAct loop 稳定后再建设。

## 立即下一步

建议从阶段 1 开始。

1. 在 `agent/pipeline.py` 中实现 helper：

```python
def _extract_metric_table_names(metrics_result: dict[str, Any]) -> list[str]:
    ...
```

它应该从以下字段中返回去重后的表名：

- 每个 metric 的 `base_table`
- 每个 metric 的 `join_tables`

2. 在调用 `search_schema()` 前提取表名：

```python
table_names = _extract_metric_table_names(metrics_result)

schema_result = await search_schema(
    query=schema_query,
    tenant_id=tenant_id,
    top_k=8,
    table_names=table_names or None,
)
```

3. 在 `tests/test_agent_pipeline.py` 中新增或更新测试，断言 `search_schema()` 收到：

```python
table_names=["orders", ...]
```
