# P1 NL2SQL 基础引擎实现说明

本文档逐文件说明本次 P1 实现的职责划分、核心代码片段、设计原因，以及 Gemini 提示词的设计思路。

## 一、整体目标

P1 的目标是搭建一个最小可用的 NL2SQL 基础引擎：

1. 从用户自然语言问题中解析 BI 查询意图。
2. 将 schema catalog 和 metrics registry 注入 SQL 生成 prompt。
3. 使用 Gemini 生成 PostgreSQL 只读 SQL。
4. 对 SQL 做安全校验和自动 `LIMIT` 兜底。
5. 可选执行 SQL，并通过 CLI 输出 intent、SQL 和结果。

整体数据流如下：

```text
用户问题
  -> Gemini 意图解析
  -> QueryIntent
  -> 注入 metrics registry + schema catalog
  -> Gemini SQL 生成
  -> 只读 SQL 校验 + 自动 LIMIT
  -> 可选 PostgreSQL 执行
  -> CLI 输出
```

## 二、`core/`：Gemini 基础能力

### `core/google_client.py`

**文件作用**

这个文件负责读取 `.env`、获取 Gemini 模型名、创建 Gemini SDK client。它把环境变量加载和 client 初始化集中起来，避免业务层到处处理 API Key。

**核心代码片段**

```python
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL_NAME", "gemini-2.5-flash")


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
```

```python
def get_client():
    load_env_file()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY. Set it in .env or the environment.")

    from google import genai
    return genai.Client(api_key=api_key)
```

**设计说明**

- 没有强依赖 `python-dotenv`，而是实现了一个轻量 `.env` 读取函数。
- `os.environ.setdefault` 表示外部环境变量优先级更高，`.env` 只作为默认值。
- `google-genai` 在 `get_client()` 内部延迟导入，这样测试不需要真实安装或调用 Gemini SDK。

### `core/stream_chat.py`

**文件作用**

这个文件封装 Gemini 的流式生成能力，提供统一的 `GeminiLLM.complete()` 和 `GeminiLLM.stream()` 接口。上层引擎只关心“给 prompt，拿文本”，不直接依赖 Gemini SDK 的 chunk 结构。

**核心代码片段**

```python
@dataclass(slots=True)
class StreamResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
```

```python
class GeminiLLM:
    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        result = await self.stream(
            prompt=prompt,
            system=system,
            max_output_tokens=max_output_tokens,
            on_token=on_token,
        )
        return result.text
```

```python
stream = await self.client.aio.models.generate_content_stream(
    model=self.model,
    contents=prompt,
    config=config,
)

async for chunk in stream:
    text = getattr(chunk, "text", None)
    if text:
        parts.append(text)
        if on_token:
            on_token(text)
```

**设计说明**

- `complete()` 复用 `stream()`，保证非流式和流式调用走同一套逻辑。
- `on_token` 是流式输出钩子，CLI 或后续 Web/SSE 层可以直接复用。
- `StreamResult` 保留 token 信息，后续 P3/P4 做成本统计时可以继续扩展。

### `core/structured_output.py`

**文件作用**

这个文件负责从 Gemini 输出中提取 JSON 对象。模型可能返回裸 JSON、Markdown fenced code block，或者 `<o>...</o>` 包裹的 JSON，这里统一兼容。

**核心代码片段**

```python
tagged = re.search(r"<o>\s*(\{.*?\})\s*</o>", value, flags=re.DOTALL | re.IGNORECASE)
if tagged:
    return tagged.group(1)

fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, flags=re.DOTALL | re.IGNORECASE)
if fenced:
    return fenced.group(1)
```

```python
def extract_json_object(text: str) -> dict[str, Any]:
    obj = json.loads(extract_json_text(text))
    if not isinstance(obj, dict):
        raise ValueError("Expected a JSON object from model output.")
    return obj
```

**设计说明**

- LLM 输出不总是严格裸 JSON，所以先提取再 `json.loads`。
- `<o>` 标签适合 prompt 中强约束输出边界。
- fenced code block 兼容 Gemini 有时返回 ```json 的情况。

## 三、`catalog/`：Schema Catalog 加载与格式化

### `catalog/loader.py`

**文件作用**

这个文件读取 `schema_catalog.json`，并把表结构格式化为适合放进 SQL 生成 prompt 的 schema 片段。

**核心代码片段**

```python
def load_schema_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> list[dict]:
    with Path(path).open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError("Schema catalog must be a list of table objects.")
    return data
```

```python
def infer_relevant_tables(catalog: list[dict], names: Iterable[str] | None = None) -> list[str]:
    requested = {name.lower() for name in names or [] if name}
    tables = all_table_names(catalog)
    if not requested:
        return tables

    matched = [table for table in tables if table.lower() in requested]
    return matched or tables
```

```python
lines.append(f"## table: {table}")
for col in entry.get("columns", []):
    nullable = "nullable" if col.get("nullable") else "not null"
    lines.append(
        f"- {col.get('name')} ({col.get('type')}, {nullable}{default}): {col_comment}"
    )
```

**设计说明**

- `load_schema_catalog()` 是文件读取边界，其他模块不用关心 JSON 文件路径和校验。
- `load_schema_snippet()` 输出的是面向 LLM 的结构化文本，而不是 Python dict，方便直接拼入 prompt。
- 如果没有匹配到指定表，会回退到所有表，避免 prompt 缺少 schema 导致 SQL 生成失败。

## 四、`engine/`：NL2SQL 核心流程

### `engine/models.py`

**文件作用**

这个文件定义 P1 引擎内部流转的数据结构：`QueryIntent` 表示解析后的用户意图，`NL2SQLResult` 表示整个 pipeline 的输出。

**核心代码片段**

```python
@dataclass(slots=True)
class QueryIntent:
    metrics: list[str] = field(default_factory=list)
    time_range: dict[str, Any] = field(default_factory=dict)
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
```

```python
@classmethod
def from_dict(cls, data: dict[str, Any]) -> "QueryIntent":
    return cls(
        metrics=_string_list(data.get("metrics")),
        time_range=data.get("time_range") if isinstance(data.get("time_range"), dict) else {},
        dimensions=_string_list(data.get("dimensions")),
        filters=_string_list(data.get("filters")),
    )
```

**设计说明**

- 使用 dataclass 保持轻量，不引入 Pydantic 运行时依赖。
- `_string_list()` 容错处理模型输出：字符串、列表、空值都能转换为稳定的 `list[str]`。
- 文件命名为 `models.py`，避免使用 `types.py` 与 Python 标准库 `types` 产生 IDE/Pylance 冲突。

### `engine/metrics.py`

**文件作用**

这个文件实现指标注册表，保存业务指标定义、SQL 表达式、可用维度和同义词。SQL 生成时会把相关指标注入 prompt。

**核心代码片段**

```python
@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    business_def: str
    sql_expr: str
    dimensions: tuple[str, ...]
    synonyms: tuple[str, ...] = ()
```

```python
Metric(
    name="gmv",
    business_def="Paid gross merchandise value.",
    sql_expr="sum(orders.amount)",
    dimensions=("region", "paid_date", "product_id", "user_id"),
    synonyms=("GMV", "sales", "..."),
)
```

```python
def prompt_block(self, names: Iterable[str] | None = None) -> str:
    selected = self.select(names or [])
    lines: list[str] = []
    for metric in selected:
        lines.append(f"- {metric.name}: {metric.business_def}")
        lines.append(f"  sql_expr: {metric.sql_expr}")
        lines.append(f"  dimensions: {', '.join(metric.dimensions)}")
    return "\n".join(lines)
```

**设计说明**

- 指标注册表把“业务口径”和“SQL 片段”固定下来，减少模型自由发挥。
- `select()` 根据 intent 中的指标名筛选相关指标；如果没有匹配，则回退给出全部指标。
- `prompt_block()` 生成面向 LLM 的指标说明文本，和 schema snippet 一起作为 SQL 生成上下文。

### `engine/intent_parser.py`

**文件作用**

这个文件负责把用户自然语言问题解析成 `QueryIntent`。它调用 Gemini，并使用 `extract_json_object()` 解析模型输出。

**核心代码片段**

```python
INTENT_SYSTEM = """
You are a BI intent parser for a PostgreSQL NL2SQL engine.
Return only JSON. Do not explain.
JSON shape:
{
  "metrics": ["metric names such as gmv, paid_orders, refund_rate"],
  "time_range": {"start": "YYYY-MM-DD or empty", "end": "YYYY-MM-DD or empty"},
  "dimensions": ["dimension names such as region, category, paid_date"],
  "filters": ["business filters in concise natural language"]
}
""".strip()
```

```python
async def parse_intent(question: str, llm) -> QueryIntent:
    raw = await llm.complete(prompt=question, system=INTENT_SYSTEM, max_output_tokens=1024)
    return QueryIntent.from_dict(extract_json_object(raw))
```

**设计说明**

- 意图解析只做结构化抽取，不直接生成 SQL，降低单次模型任务复杂度。
- `llm` 作为参数传入，便于测试时注入 `FakeLLM`，不需要真实调用 Gemini。
- `max_output_tokens=1024` 足够输出短 JSON，同时避免模型长篇解释。

### `engine/sql_generator.py`

**文件作用**

这个文件负责根据用户问题、解析后的 intent、schema catalog 和 metrics registry 生成 SQL，并做基础清洗与安全校验。

**核心代码片段**

```python
SQL_SYSTEM = """
You generate safe PostgreSQL SELECT SQL for a BI NL2SQL engine.
Rules:
- Return only one SQL statement.
- Use only tables and columns shown in the schema context.
- Generate read-only SELECT or WITH ... SELECT statements only.
- Prefer metric definitions from the metrics registry.
- Include GROUP BY for every non-aggregate selected dimension.
- Do not include markdown, explanations, DDL, DML, comments, or semicolon.
""".strip()
```

```python
prompt = f"""
Question:
{question}

Parsed intent:
metrics: {intent.metrics}
time_range: {intent.time_range}
dimensions: {intent.dimensions}
filters: {intent.filters}

Metrics registry:
{metrics.prompt_block(intent.metrics)}

Schema catalog:
{schema}
""".strip()
```

```python
raw = await llm.complete(prompt=prompt, system=SQL_SYSTEM, max_output_tokens=2048)
sql = ensure_limit(_extract_sql(raw))
assert_readonly_sql(sql)
return sql
```

**设计说明**

- SQL 生成 prompt 同时包含用户原问题、结构化 intent、指标口径和 schema，避免模型只凭自然语言猜表猜字段。
- `_extract_sql()` 兼容模型返回 fenced SQL 的情况，并统一压缩空白、移除末尾分号。
- `ensure_limit()` 和 `assert_readonly_sql()` 在模型输出后兜底，防止结果过大或出现危险 SQL。

### `engine/executor.py`

**文件作用**

这个文件提供 SQL 安全校验、自动 `LIMIT` 和可选 PostgreSQL 执行能力。

**核心代码片段**

```python
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|truncate|create|grant|revoke|copy|call|execute)\b",
    re.IGNORECASE,
)
```

```python
def assert_readonly_sql(sql: str) -> None:
    normalized = sql.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", normalized, flags=re.IGNORECASE):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    if FORBIDDEN_SQL.search(normalized):
        raise ValueError("Only read-only SQL is allowed.")
    if ";" in normalized:
        raise ValueError("Multiple SQL statements are not allowed.")
```

```python
async def execute_readonly_sql(sql: str, dsn: str | None = None, timeout_ms: int = 10_000) -> list[dict[str, Any]]:
    assert_readonly_sql(sql)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"set statement_timeout = {int(timeout_ms)}")
        rows = await conn.fetch(sql)
        return [dict(row) for row in rows]
    finally:
        await conn.close()
```

**设计说明**

- 执行前再次调用 `assert_readonly_sql()`，即使 SQL 不是模型生成的也会受保护。
- 只允许 `SELECT` 或 `WITH` 开头，显式禁止常见 DDL/DML/权限类语句。
- `statement_timeout` 防止长查询拖死数据库。
- `asyncpg` 延迟导入，文档测试和纯 SQL 生成不要求安装数据库依赖。

### `engine/pipeline.py`

**文件作用**

这个文件把 P1 的各个步骤串成一个完整 pipeline，是上层 CLI 调用的主入口。

**核心代码片段**

```python
async def run_nl2sql(
    question: str,
    *,
    execute: bool = False,
    catalog_path: str = "schema_catalog.json",
    llm=None,
    dsn: str | None = None,
) -> NL2SQLResult:
    llm = llm or GeminiLLM()
    catalog = load_schema_catalog(catalog_path)
    metrics = MetricRegistry.default()
```

```python
intent = await parse_intent(question, llm=llm)
sql = await generate_sql(
    question=question,
    intent=intent,
    catalog=catalog,
    metrics=metrics,
    llm=llm,
)
rows = await execute_readonly_sql(sql, dsn=dsn) if execute else []
```

**设计说明**

- `run_nl2sql()` 是稳定编排层，隐藏具体步骤细节。
- `execute=False` 默认只生成 SQL，不访问数据库，更适合开发调试。
- `llm` 可注入，方便测试和未来替换模型。

## 五、CLI 入口

### `main.py`

**文件作用**

这个文件提供命令行入口，支持单次问题调用和交互式 REPL。

**核心代码片段**

```python
async def run_once(question: str, execute: bool = False) -> None:
    result = await run_nl2sql(question, execute=execute)
    print("\n[intent]")
    print(result.intent)
    print("\n[sql]")
    print(result.sql)
```

```python
parser.add_argument("question", nargs="?", help="Natural language BI question.")
parser.add_argument(
    "--execute",
    action="store_true",
    help="Execute generated SQL against DATABASE_URL/POSTGRES_DSN.",
)
```

**设计说明**

- 不传问题时进入 REPL，适合连续调试。
- 传入问题时单次执行，适合脚本化调用。
- `--execute` 明确区分“只生成 SQL”和“真正查库”，降低误操作风险。

## 六、测试文件

### `tests/test_p1_engine.py`

**文件作用**

这个文件验证 P1 核心行为，包括 JSON 提取、意图解析、SQL 生成上下文、只读 SQL 防护、自动 LIMIT 和 schema 过滤。

**核心代码片段**

```python
class FakeLLM:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def complete(self, prompt, system="", **kwargs):
        self.calls.append({"prompt": prompt, "system": system, **kwargs})
        return self.text
```

```python
intent = await parse_intent("...", llm=llm)

self.assertEqual(intent.metrics, ["gmv"])
self.assertEqual(intent.dimensions, ["region"])
self.assertIn("JSON", llm.calls[0]["system"])
```

```python
with self.assertRaises(ValueError):
    assert_readonly_sql("delete from orders")
```

**设计说明**

- `FakeLLM` 让测试不依赖真实 Gemini API。
- 测试关注行为，不检查 SDK 细节。
- 安全相关行为单独测试，例如 DML 阻断和自动 `LIMIT`。

## 七、提示词设计说明

### `INTENT_SYSTEM` 的设计

`INTENT_SYSTEM` 的核心目标是让 Gemini 做“意图抽取”，而不是回答问题或生成 SQL。

关键约束：

```text
Return only JSON. Do not explain.
```

这个约束是为了降低解析失败率。后续代码会调用 `extract_json_object()`，如果模型输出解释性文字过多，就需要额外清洗。

输出结构被固定为：

```json
{
  "metrics": [],
  "time_range": {"start": "", "end": ""},
  "dimensions": [],
  "filters": []
}
```

这样设计的原因：

- `metrics` 对接 `MetricRegistry`。
- `time_range` 为后续时间过滤和 SQL 生成保留结构化字段。
- `dimensions` 用于决定 `GROUP BY`。
- `filters` 保留业务过滤条件，由 SQL 生成阶段结合 schema 再落成 SQL。

### `SQL_SYSTEM` 的设计

`SQL_SYSTEM` 的核心目标是让 Gemini 只生成可控、可执行、只读的 PostgreSQL SQL。

关键约束：

```text
- Return only one SQL statement.
- Use only tables and columns shown in the schema context.
- Generate read-only SELECT or WITH ... SELECT statements only.
- Prefer metric definitions from the metrics registry.
- Include GROUP BY for every non-aggregate selected dimension.
- Do not include markdown, explanations, DDL, DML, comments, or semicolon.
```

这些规则分别解决以下问题：

- **只返回一条 SQL**：避免多语句执行风险。
- **只使用 schema context 中的字段**：减少模型幻觉字段。
- **只读 SELECT/WITH**：防止写库、删表、改权限。
- **优先使用 metrics registry**：让 GMV、退款率等指标遵循业务口径。
- **维度字段必须 GROUP BY**：减少聚合 SQL 的语法错误。
- **不输出解释、Markdown、分号**：减少后处理复杂度和多语句风险。

## 八、Prompt 上下文拼装方式

SQL 生成阶段的 prompt 包含四类信息：

```text
Question:
用户原始问题

Parsed intent:
结构化意图

Metrics registry:
指标口径和 SQL 表达式

Schema catalog:
可用表、字段、类型、注释
```

这样拆分的原因是：

1. 用户原始问题保留完整语义。
2. Parsed intent 提供结构化中间层，减少模型重新理解问题的成本。
3. Metrics registry 约束指标口径。
4. Schema catalog 约束可用表字段。

## 九、当前实现边界

当前 P1 是基础引擎，不包含以下能力：

- 不做 schema/metrics 的向量召回。
- 不做复杂 SQL AST 校验。
- 不做 LangGraph 多节点编排。
- 不做审计日志、成本统计和 Web UI。

这些能力更适合放到后续 P2/P3/P4 中继续扩展。
