import json
from pathlib import Path
from typing import Iterable


DEFAULT_CATALOG_PATH = "schema_catalog.json"


def load_schema_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> list[dict]:
    with Path(path).open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError("Schema catalog must be a list of table objects.")
    return data


def all_table_names(catalog: list[dict]) -> list[str]:
    return [entry["table"] for entry in catalog if "table" in entry]


def infer_relevant_tables(catalog: list[dict], names: Iterable[str] | None = None) -> list[str]:
    requested = {name.lower() for name in names or [] if name}
    tables = all_table_names(catalog)
    if not requested:
        return tables

    matched = [table for table in tables if table.lower() in requested]
    return matched or tables


def load_schema_snippet(catalog: list[dict], table_names: Iterable[str] | None = None) -> str:
    """Format selected schema catalog entries for SQL generation prompts."""
    selected = set(infer_relevant_tables(catalog, table_names))
    lines: list[str] = []

    for entry in catalog:
        table = entry.get("table")
        if table not in selected:
            continue

        comment = entry.get("comment") or ""
        lines.append(f"## table: {table}")
        if comment:
            lines.append(f"comment: {comment}")

        for col in entry.get("columns", []):
            nullable = "nullable" if col.get("nullable") else "not null"
            default = f", default={col.get('default')}" if col.get("default") else ""
            col_comment = col.get("comment") or ""
            lines.append(
                f"- {col.get('name')} ({col.get('type')}, {nullable}{default}): {col_comment}"
            )
        lines.append("")

    return "\n".join(lines).strip()
