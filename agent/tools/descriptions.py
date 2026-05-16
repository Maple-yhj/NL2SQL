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


TOOL_DESCRIPTIONS = {
    "search_metrics": SEARCH_METRICS_DESCRIPTION,
    "search_schema": SEARCH_SCHEMA_DESCRIPTION,
    "validate_sql": VALIDATE_SQL_DESCRIPTION,
}


def get_tool_description(name: str) -> str:
    try:
        return TOOL_DESCRIPTIONS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown tool description: {name}") from exc
