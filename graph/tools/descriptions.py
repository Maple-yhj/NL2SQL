"""Tool descriptions used by the P2 NL2SQL agent."""

SEARCH_METRICS_DESCRIPTION = """
工具作用:
根据用户问题或指标关键词，在指标语义索引中检索相关业务指标定义。该工具用于在生成 SQL 前明确业务指标的计算口径，例如指标名、展示名、业务定义、SQL 聚合表达式、基础表、时间字段、可用维度、默认过滤条件、关联表、禁用口径和同义词。

什么时候使用:
- 当用户问题包含业务指标、KPI 或统计口径时使用，例如 GMV、销售额、成交额、支付订单数、退款率、复购率、客单价、转化率。
- 当 Agent 需要把自然语言里的业务词转换成明确指标定义时使用。
- 通常在 generate_sql 之前使用，并且常与 search_schema 配合使用。

什么时候不能使用:
- 不能用于执行 SQL 或查询真实业务数据。
- 不能用于校验 SQL 是否安全。
- 不能用于查找普通表字段结构；字段和表结构应使用 search_schema。
- 不能用于解释查询结果。
- 不能替代数据库权限控制或 SQL 执行层校验。

返回参数含义:
- ok: 工具调用是否成功。
- query: 本次用于检索的原始查询文本。
- tenant_id: 租户 ID，用于隔离不同租户的指标定义。
- metrics: 命中的指标列表。
- message: 工具执行结果说明。

metrics 中每个对象的字段含义:
- metric_name: 指标内部名称，例如 gmv。
- display_name: 指标展示名称，例如 GMV。
- business_def: 指标业务定义，说明该指标在业务上如何理解。
- sql_expr: 指标对应的 SQL 表达式，通常是聚合表达式。
- base_table: 指标主要依赖的基础表。
- time_column: 指标默认使用的时间字段。
- dimensions: 该指标支持的分析维度。
- filters: 该指标默认需要附加的过滤条件。
- join_tables: 生成 SQL 时可能需要的关联表或 JOIN 线索。
- forbidden: 该指标禁止使用的错误口径、条件或解释。
- synonyms: 指标同义词，用于处理自然语言里的不同说法。
- score: 语义检索相关度分数，分数越高表示越相关。
""".strip()


SEARCH_SCHEMA_DESCRIPTION = """
工具作用:
根据用户问题、指标信息或字段关键词，在 schema 语义索引中检索相关表和字段。该工具用于让 Agent 在生成 SQL 前获得真实可用的数据库结构，包括表名、表注释、字段名、字段类型、字段注释、样例值和相关度分数。

什么时候使用:
- 当 Agent 需要确定 SQL 应使用哪些表、哪些字段、哪些 JOIN 线索时使用。
- 当用户问题包含维度、筛选条件或字段语义时使用，例如地区、品类、支付时间、退款状态、用户等级。
- 当 search_metrics 已返回指标定义后，用于检索指标涉及的 base_table、join_tables 和相关字段。
- 通常在 generate_sql 之前使用，也可用于为 validate_sql 准备 allowed_tables。

什么时候不能使用:
- 不能用于查询业务指标定义；指标口径应使用 search_metrics。
- 不能用于执行 SQL 或返回真实查询结果。
- 不能用于校验 SQL 是否安全；SQL 安全校验应使用 validate_sql。
- 不能用于解释查询结果。
- 不能替代指标注册表中的业务口径。

返回参数含义:
- ok: 工具调用是否成功。
- query: 本次用于检索的原始查询文本。
- tenant_id: 租户 ID，用于隔离不同租户的 schema 索引。
- schema: 命中的表结构列表。
- message: 工具执行结果说明。

schema 中每个对象的字段含义:
- table_name: 表名。
- table_comment: 表说明或业务注释。
- score: 该表与查询文本的相关度分数。
- columns: 该表下命中的相关字段列表。

columns 中每个对象的字段含义:
- column_name: 字段名。
- data_type: 字段类型。
- nullable: 字段是否允许为空。
- default: 字段默认值。
- comment: 字段说明或业务注释。
- sample_values: 字段样例值，用于帮助 Agent 理解字段含义和取值形态。
- score: 该字段与查询文本的相关度分数。
""".strip()


GENERATE_SQL_DESCRIPTION = """
工具作用:
根据当前请求中受控保存的用户问题、解析意图、指标上下文、schema 上下文和最近一次校验反馈生成一条候选只读 SQL。该工具调用现有 SQL generator，并将候选 SQL 写回状态供后续安全校验使用。

什么时候使用:
- 当 search_metrics 与 search_schema 已返回生成 SQL 所需的业务和字段上下文时使用。
- 当上一次候选 SQL 未通过 validate_sql，需要依据校验反馈重新生成时使用。

安全边界:
- planner 不提供 SQL、意图、schema、授权表或重试反馈；这些输入均由状态上下文注入。
- 生成的 SQL 仍必须经过 validate_sql，不能直接执行。

返回参数含义:
- ok: 是否成功生成候选 SQL。
- sql: 最新生成、等待校验的候选 SQL。
- message: 工具执行结果说明。
""".strip()


VALIDATE_SQL_DESCRIPTION = """
工具作用:
校验模型生成的 SQL 是否安全、可控，并符合只读查询要求。该工具会解析 SQL AST，阻止多语句、非 SELECT 查询、DDL/DML、危险操作和未授权表访问，并补充缺失的 LIMIT 或降低过大的 LIMIT。该工具只做校验和规范化，不执行 SQL。

什么时候使用:
- 必须在 generate_sql 之后、execute_readonly_sql 之前使用。
- 当 Agent 需要确认 SQL 是否只读、安全、只访问允许表时使用。
- 当需要把 SQL 校验失败原因返回给 Agent，并要求 Agent 重新生成 SQL 时使用。
- 当需要生成 normalized_sql 作为后续执行 SQL 时使用。

什么时候不能使用:
- 不能用于生成 SQL。
- 不能用于执行 SQL。
- 不能用于判断业务指标口径是否正确；业务口径应由 search_metrics 提供。
- 不能用于判断字段语义是否相关；字段语义应由 search_schema 提供。
- 不能用于判断查询结果是否正确。
- 不能替代 execute_readonly_sql 中的最终执行前安全兜底。
- 不能替代数据库权限、RLS 或租户隔离策略。

返回参数含义:
- ok: SQL 是否通过校验。true 表示可进入执行阶段，false 表示必须阻止执行或重新生成 SQL。
- sql: 原始 SQL。
- normalized_sql: 规范化后的 SQL，例如补充或调整 LIMIT 后的 SQL。
- tenant_id: 租户 ID。
- tables: 从 SQL AST 中解析出的表名列表。
- columns: 从 SQL AST 中解析出的字段名列表。
- limit: 最终使用的 LIMIT 值；如果无法得到合法 LIMIT，则可能为 None。
- violations: 阻止 SQL 执行的问题列表。
- warnings: 非阻断性提醒，例如自动补充 LIMIT 或降低过大的 LIMIT。
- message: 整体校验结果说明。

violations 中每个对象的字段含义:
- code: 机器可读的问题代码，例如 parse_error、multiple_statements、not_select、not_readonly、table_not_allowed、invalid_limit。
- message: 人类可读的问题说明，可用于提示 Agent 修正 SQL。
""".strip()


EXECUTE_SQL_DESCRIPTION = """
工具名称: execute_sql

工具用途:
执行已经生成并校验过的只读 SQL，返回真实数据库查询结果。该工具面向 P2 agent 的最终执行阶段，通常在 generate_sql 和 validate_sql 之后使用。

使用时机:
- 当 validate_sql 返回 ok=true，并且用户或 pipeline 明确要求 execute=true 时使用。
- 优先传入 validate_sql 返回的 normalized_sql，确保补充 LIMIT 或收缩 LIMIT 后的 SQL 被真正执行。
- 当需要把 SQL 查询结果交给 explain_result 或最终回答用户时使用。

安全边界:
- execute_sql 内部会再次调用 validate_sql，作为执行前的兜底校验。
- 可以传入 allowed_tables，限制 SQL 只能访问 search_schema 返回的表。
- 只允许 SELECT / WITH 查询，不允许 DDL、DML、多语句或危险操作。
- 不负责生成 SQL；SQL 生成应使用 generate_sql。
- 不负责解释查询结果；结果解释应交给 explain_result。
- 当前 tenant_id 主要用于结果追踪和校验上下文，不等价于强租户隔离。

输入参数:
- sql: 要执行的 SQL。推荐传入 normalized_sql。
- tenant_id: 租户 ID。
- dsn: 可选数据库连接串；为空时读取 DATABASE_URL 或 POSTGRES_DSN。
- timeout_ms: 可选语句超时时间，默认 10000。
- max_limit: 可选最大 LIMIT，默认 1000。
- allowed_tables: 可选允许访问的表名列表，通常来自 search_schema。

返回字段:
- ok: 执行是否成功。
- sql: 原始传入 SQL。
- normalized_sql: validate_sql 规范化后的 SQL。
- tenant_id: 租户 ID。
- rows: 查询结果行列表，每行是 dict。
- row_count: rows 的数量。
- validation: 执行前 validate_sql 的完整结果。
- violations: 当 SQL 校验失败时返回违规项。
- message: 成功或失败说明。

失败场景:
- 缺少 DATABASE_URL / POSTGRES_DSN。
- SQL 没有通过 validate_sql。
- 数据库连接失败、执行超时、SQL 运行时报错。
""".strip()


EXPLAIN_RESULT_DESCRIPTION = """
工具名称: explain_result

工具用途:
将 execute_sql 返回的 rows 转成面向用户的简短解释。当前版本是规则版解释器，不调用 LLM；后续可以在同一接口下升级为 LLM 解释。

使用时机:
- 当 SQL 已通过 validate_sql，且 execute_sql 已成功返回 rows 后使用。
- 当 pipeline 或 ReAct loop 需要把结构化查询结果转换为自然语言回答时使用。
- 适合解释聚合结果、分组结果和少量明细预览。

输入参数:
- question: 用户原始问题。
- sql: 实际执行或规范化后的 SQL。
- rows: 查询结果行列表，每行是 dict。
- metrics_result: search_metrics 返回的指标上下文，用于补充指标名称。
- llm: 可选参数，当前规则版不使用。
- max_preview_rows: 可选预览行数，默认 5。

返回字段:
- ok: 是否成功生成解释。
- question: 用户原始问题。
- sql: 输入 SQL。
- row_count: 查询结果行数。
- columns: 结果字段列表。
- preview_rows: 用于解释的前几行结果。
- explanation: 生成的自然语言解释。
- message: 成功或失败说明。

边界:
- 不执行 SQL；执行应使用 execute_sql。
- 不校验 SQL；校验应使用 validate_sql。
- 不尝试推断查询结果之外的业务结论，只解释 rows 中真实存在的数据。
""".strip()


TOOL_DESCRIPTIONS = {
    "search_metrics": SEARCH_METRICS_DESCRIPTION,
    "search_schema": SEARCH_SCHEMA_DESCRIPTION,
    "generate_sql": GENERATE_SQL_DESCRIPTION,
    "validate_sql": VALIDATE_SQL_DESCRIPTION,
    "execute_sql": EXECUTE_SQL_DESCRIPTION,
    "explain_result": EXPLAIN_RESULT_DESCRIPTION,
}


def get_tool_description(name: str) -> str:
    try:
        return TOOL_DESCRIPTIONS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown tool description: {name}") from exc
