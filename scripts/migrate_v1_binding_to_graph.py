"""Convert a v1 semantic binding JSON plus catalog JSON into a reviewable graph draft.

This intentionally writes a draft only; activation remains an explicit API action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from data_agent.datasources.models import SemanticBindingRecord
from data_agent.relationships.compat import normalize_binding_graph
from data_agent.relationships.models import RelationshipGraphDraft
from data_agent.tools.schemas import CatalogSnapshot


def build_draft(*, binding: SemanticBindingRecord, catalog: CatalogSnapshot) -> RelationshipGraphDraft:
    """Create an inert v2 draft from a legacy binding; never activate it."""

    graph = normalize_binding_graph(binding, catalog)
    return RelationshipGraphDraft(
        graph_id=f"migration-{binding.binding_id}", tenant_id=binding.tenant_id,
        source_id=binding.source_id, source_snapshot_version=binding.source_snapshot_version,
        schema_fingerprint=catalog.schema_fingerprint, revision=1, status="draft",
        nodes=graph.nodes, edges=graph.edges, components=graph.components, route_rules=graph.route_rules,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("binding", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--output", type=Path, help="Destination draft JSON (required with --execute).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Print the draft JSON without writing or activating it.")
    mode.add_argument("--execute", action="store_true", help="Write the draft JSON; never activates a binding.")
    args = parser.parse_args()
    binding = SemanticBindingRecord.model_validate_json(args.binding.read_text())
    catalog = CatalogSnapshot.model_validate_json(args.catalog.read_text())
    draft = build_draft(binding=binding, catalog=catalog)
    document = draft.model_dump_json(indent=2) + "\n"
    if args.preview:
        print(document, end="")
        return
    if args.output is None:
        parser.error("--output is required with --execute")
    args.output.write_text(document)


if __name__ == "__main__":
    main()
