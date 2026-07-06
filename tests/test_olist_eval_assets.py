import json
import re
import unittest
from pathlib import Path

from catalog.domain_loader import load_domain_profile
from catalog.domain_resolver import resolve_domain_context
from catalog.loader import load_schema_catalog
from engine.models import QueryIntent


METRIC_BASE_TABLES = {
    "gmv": "olist_order_items_dataset",
    "orders": "olist_order_items_dataset",
    "avg_item_price": "olist_order_items_dataset",
    "avg_review_score": "olist_order_reviews_dataset",
}


class OlistEvalAssetTests(unittest.TestCase):
    def test_jsonl_markdown_schema_and_domain_profile_stay_in_sync(self):
        rows = [
            json.loads(line)
            for line in Path("evals/olist_questions.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        markdown = Path("evals/olist_questions.md").read_text(encoding="utf-8")
        markdown_ids = re.findall(
            r"`(olist_(?:metric|detail|join|payment|review|logistics|geo|tenant|followup)_\d+)`",
            markdown,
        )
        schema_tables = {
            entry["table"]
            for entry in load_schema_catalog("schema_catalog.json")
            if entry.get("table")
        }
        profile = load_domain_profile("olist")

        self.assertEqual(len(rows), 48)
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        jsonl_ids = {row["id"] for row in rows}
        self.assertTrue(set(markdown_ids).issubset(jsonl_ids))

        for row in rows:
            self.assertTrue(set(row["expected_tables"]).issubset(schema_tables), row["id"])
            metrics = [
                {
                    "metric_name": metric,
                    "base_table": METRIC_BASE_TABLES[metric],
                    "join_tables": [],
                }
                for metric in row["expected_metrics"]
                if metric in METRIC_BASE_TABLES
            ]
            resolution = resolve_domain_context(
                profile=profile,
                question=row["question"],
                intent=QueryIntent(
                    metrics=row["expected_metrics"],
                    dimensions=row["expected_dimensions"],
                ),
                metrics_result={"metrics": metrics},
            )
            missing_tables = [
                table for table in row["expected_tables"]
                if table not in resolution.required_tables
            ]
            self.assertEqual(missing_tables, [], row["id"])


if __name__ == "__main__":
    unittest.main()
