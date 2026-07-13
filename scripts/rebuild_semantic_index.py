"""Build a deterministic canonical-only semantic index from a Domain Pack."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime.maintenance import (
    build_semantic_index,
    rebuild_semantic_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    path = rebuild_semantic_index(
        project_root=arguments.project_root,
        output_path=arguments.output,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
