from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from data_agent.runtime import load_domain_pack, load_enterprise_binding
from tests.support.olist_relational_evaluator import (
    EvaluationPrincipal,
    evaluate_raw_case,
)


GOLDEN_PRINCIPALS = {
    "seller": EvaluationPrincipal(tenant_id="seller-42", roles=("seller",)),
    "admin": EvaluationPrincipal(tenant_id="admin-eval", roles=("admin",)),
}


def main() -> None:
    domain_pack = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
    enterprise_binding = load_enterprise_binding(
        ROOT / "packs" / "enterprises" / "olist"
    )
    oracle_path = ROOT / "tests" / "fixtures" / "olist_golden_oracle.json"
    document = json.loads(oracle_path.read_text(encoding="utf-8"))
    if set(document) != {case.id for case in domain_pack.spec.evals}:
        raise ValueError("golden oracle and raw commerce eval IDs differ")

    for case in domain_pack.spec.evals:
        if document[case.id]["raw_case"] != case.model_dump(mode="json"):
            raise ValueError(f"stale raw eval snapshot: {case.id}")
        for oracle_label, principal in GOLDEN_PRINCIPALS.items():
            evaluated = evaluate_raw_case(
                case,
                domain_pack,
                enterprise_binding,
                principal=principal,
            )
            document[case.id][oracle_label]["columns"] = list(evaluated.columns)
            document[case.id][oracle_label]["rows"] = [
                list(row) for row in evaluated.rows
            ]

    oracle_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
