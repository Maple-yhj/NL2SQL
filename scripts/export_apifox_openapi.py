"""Deterministically export the inert Data Agent FastAPI contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from api.app import create_app


OUTPUT_PATH = PROJECT_ROOT / "docs" / "apifox-openapi.json"


def export_openapi(output_path: str | Path = OUTPUT_PATH) -> Path:
    specification = create_app().openapi()
    specification["info"] = {
        "title": "Data Agent API",
        "version": "1.0.0",
        "description": (
            "Governed Data Agent Runtime API with JWT authentication, "
            "strict plan/preview/execute requests, and conversation management."
        ),
    }
    specification["servers"] = [
        {
            "url": "http://localhost:8000",
            "description": "Local development",
        }
    ]
    _normalize_security_scheme(specification)

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            specification,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def _normalize_security_scheme(specification: dict[str, Any]) -> None:
    components = specification.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    bearer = security_schemes.pop("HTTPBearer", None)
    if bearer is not None:
        security_schemes["BearerAuth"] = {
            **bearer,
            "bearerFormat": "JWT",
            "description": "Access token returned by the login or refresh endpoint.",
        }
    for path_item in specification.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict) and operation.get("security") == [
                {"HTTPBearer": []}
            ]:
                operation["security"] = [{"BearerAuth": []}]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    arguments = parser.parse_args(argv)
    print(export_openapi(arguments.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
