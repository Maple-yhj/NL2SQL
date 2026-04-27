from catalog.loader import load_schema_catalog
from core.stream_chat import GeminiLLM
from engine.executor import execute_readonly_sql
from engine.intent_parser import parse_intent
from engine.metrics import MetricRegistry
from engine.sql_generator import generate_sql
from engine.models import NL2SQLResult


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

    intent = await parse_intent(question, llm=llm)
    sql = await generate_sql(
        question=question,
        intent=intent,
        catalog=catalog,
        metrics=metrics,
        llm=llm,
    )
    rows = await execute_readonly_sql(sql, dsn=dsn) if execute else []

    return NL2SQLResult(question=question, intent=intent, sql=sql, rows=rows)
