from __future__ import annotations

from graph.tools.contracts import (
    ContextualizeQuestionInput,
    ContextualizeQuestionOutput,
    ExecuteSqlInput,
    ExecuteSqlOutput,
    ExplainResultInput,
    ExplainResultOutput,
    GenerateSqlInput,
    GenerateSqlOutput,
    PrepareSqlInput,
    PrepareSqlOutput,
    ResolveDomainRulesInput,
    ResolveDomainRulesOutput,
    SearchMetricsInput,
    SearchMetricsOutput,
    SearchSchemaInput,
    SearchSchemaOutput,
    ToolExample,
    ToolSpec,
    ValidateSqlInput,
    ValidateSqlOutput,
)


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._aliases: dict[str, str] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        name = spec.name.strip()
        if name in self._specs or name in self._aliases:
            raise ValueError(f"Tool already registered: {name}")
        for alias in spec.aliases:
            key = alias.strip()
            if key in self._specs or key in self._aliases:
                raise ValueError(f"Tool alias already registered: {key}")
        self._specs[name] = spec
        for alias in spec.aliases:
            self._aliases[alias.strip()] = name

    def get(self, name: str) -> ToolSpec:
        key = name.strip()
        key = self._aliases.get(key, key)
        try:
            return self._specs[key]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name.strip()}") from exc

    def canonical_name(self, name: str) -> str:
        key = name.strip()
        canonical = self._aliases.get(key, key)
        if canonical not in self._specs:
            raise KeyError(f"Unknown tool: {key}")
        return canonical

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)


def default_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="contextualize_question",
                description="Rewrite a follow-up BI question into a standalone question.",
                input_keys=("question", "conversation_history", "user_memories"),
                output_keys=("contextualized_question",),
                input_schema=ContextualizeQuestionInput,
                output_schema=ContextualizeQuestionOutput,
                requires_llm=True,
                examples=(
                    ToolExample(
                        description="Rewrite a follow-up question using prior context.",
                        input={"question": "Then split by state.", "conversation_history": []},
                        output={"contextualized_question": "Show the prior metric split by state."},
                    ),
                ),
            ),
            ToolSpec(
                name="search_metrics",
                aliases=("metric.search",),
                description="Retrieve metric definitions relevant to the current question.",
                input_keys=("question", "tenant_id"),
                output_keys=("metrics_result", "table_names", "domain_context", "domain_constraints"),
                input_schema=SearchMetricsInput,
                output_schema=SearchMetricsOutput,
                requires_embeddings=True,
                examples=(
                    ToolExample(
                        description="Find metric definitions for a GMV question.",
                        input={"question": "2018 GMV by month", "tenant_id": "admin"},
                        output={"table_names": ["olist_order_items_dataset"]},
                    ),
                ),
            ),
            ToolSpec(
                name="resolve_domain_rules",
                description="Resolve domain-specific tables, joins, and SQL rules.",
                input_keys=("question", "intent", "metrics_result"),
                output_keys=("domain_context", "domain_constraints", "table_names"),
                input_schema=ResolveDomainRulesInput,
                output_schema=ResolveDomainRulesOutput,
                examples=(
                    ToolExample(
                        description="Resolve OList hard rules for a payment summary.",
                        input={"question": "各支付方式的订单数和支付金额合计"},
                        output={"table_names": ["olist_order_payments_dataset"]},
                    ),
                ),
            ),
            ToolSpec(
                name="search_schema",
                aliases=("schema.search",),
                description="Retrieve authorized schema context for SQL generation.",
                input_keys=("question", "tenant_id", "table_names"),
                output_keys=("schema_result", "allowed_tables"),
                input_schema=SearchSchemaInput,
                output_schema=SearchSchemaOutput,
                requires_embeddings=True,
                examples=(
                    ToolExample(
                        description="Retrieve schema columns for selected tables.",
                        input={"question": "orders by state", "tenant_id": "admin"},
                        output={"allowed_tables": ["olist_orders_dataset"]},
                    ),
                ),
            ),
            ToolSpec(
                name="generate_sql",
                aliases=("sql.generate",),
                description="Generate candidate PostgreSQL SQL from intent, metrics, and schema.",
                input_keys=("question", "intent", "metrics_result", "schema_result"),
                output_keys=("candidate_sql",),
                input_schema=GenerateSqlInput,
                output_schema=GenerateSqlOutput,
                requires_llm=True,
                risk_level="medium",
                examples=(
                    ToolExample(
                        description="Generate a read-only aggregate SQL query.",
                        input={"question": "2018 GMV by month"},
                        output={"candidate_sql": "SELECT DATE_TRUNC('month', shipping_limit_date) AS month ..."},
                    ),
                ),
            ),
            ToolSpec(
                name="prepare_sql",
                aliases=("sql.prepare",),
                description="Validate SQL, apply domain rules, and produce tenant-scoped executable SQL.",
                input_keys=("candidate_sql", "tenant_id", "allowed_tables"),
                output_keys=("validated_sql", "validation_result", "executable_sql"),
                input_schema=PrepareSqlInput,
                output_schema=PrepareSqlOutput,
                risk_level="medium",
                examples=(
                    ToolExample(
                        description="Validate and tenant-scope generated SQL.",
                        input={"sql": "SELECT SUM(price) FROM olist_order_items_dataset", "tenant_id": "admin"},
                        output={"executable_sql": "SELECT SUM(price) FROM olist_order_items_dataset LIMIT 1000"},
                    ),
                ),
            ),
            ToolSpec(
                name="validate_sql",
                aliases=("sql.validate",),
                description="Validate and normalize a read-only SQL query inside an authorized table scope.",
                input_keys=("sql", "tenant_id", "allowed_tables", "max_limit"),
                output_keys=("normalized_sql", "violations", "warnings"),
                input_schema=ValidateSqlInput,
                output_schema=ValidateSqlOutput,
                risk_level="medium",
                examples=(
                    ToolExample(
                        description="Reject SQL outside the authorized table scope.",
                        input={"sql": "SELECT * FROM private_table", "tenant_id": "admin"},
                        output={"ok": False, "violations": [{"code": "table_not_allowed"}]},
                    ),
                ),
            ),
            ToolSpec(
                name="execute_sql",
                aliases=("db.execute_readonly",),
                description="Execute prepared read-only SQL against the query database.",
                input_keys=("validated_sql", "tenant_id"),
                output_keys=("execution_result", "rows"),
                input_schema=ExecuteSqlInput,
                output_schema=ExecuteSqlOutput,
                requires_db=True,
                risk_level="high",
                side_effects="read",
                examples=(
                    ToolExample(
                        description="Execute a prepared read-only SQL statement.",
                        input={"validated_sql": "SELECT 1", "tenant_id": "admin"},
                        output={"ok": True, "rows": [{"?column?": 1}]},
                    ),
                ),
            ),
            ToolSpec(
                name="explain_result",
                aliases=("answer.explain",),
                description="Explain aggregate SQL result rows.",
                input_keys=("question", "validated_sql", "rows", "metrics_result"),
                output_keys=("answer",),
                input_schema=ExplainResultInput,
                output_schema=ExplainResultOutput,
                requires_llm=True,
                examples=(
                    ToolExample(
                        description="Summarize aggregate result rows.",
                        input={"question": "show gmv", "rows": [{"gmv": 100}]},
                        output={"answer": "GMV is 100."},
                    ),
                ),
            ),
            ToolSpec(
                name="explain_table_result",
                aliases=("answer.explain_table",),
                description="Explain detailed table result rows without repeating every record.",
                input_keys=("question", "validated_sql", "rows", "metrics_result"),
                output_keys=("answer",),
                input_schema=ExplainResultInput,
                output_schema=ExplainResultOutput,
                requires_llm=True,
                examples=(
                    ToolExample(
                        description="Describe detailed result rows without copying every row.",
                        input={"question": "list recent orders", "rows": [{"order_id": "1"}]},
                        output={"answer": "The result contains recent order records."},
                    ),
                ),
            ),
        ]
    )
