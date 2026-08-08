# Data Analysis Agent migration baseline

Recorded on 2026-08-08 before implementation changes on branch `Agent`.

## Protected pre-existing work

The following tracked files were already modified before this migration and belong to the user. They must not be reset, overwritten, or broadly reformatted:

- `frontend/src/relationships/RelationshipGraphEditor.tsx`
- `frontend/src/relationships/relationshipGraphState.test.ts`
- `frontend/src/relationships/relationshipGraphState.ts`
- `frontend/src/styles.css`

The pre-existing tracked diff contained 2,121 insertions and 91 deletions across those four files. The following paths were also already untracked and are protected pending explicit audit: `.env.codex-backup-20260704204355`, `.impeccable/`, `.pnpm-store/`, `docs/superpowers/.DS_Store`, the two 2026-08-08 design/implementation documents, and `frontend/pnpm-workspace.yaml`. Secret-bearing paths were not opened.

Evidence commands:

```text
git branch --show-current
git status --short
git diff --stat
git diff --cached --stat
```

## Toolchain and installation baseline

- Python: 3.12.5 from `.venv/bin/python`
- Node.js: v26.5.0
- npm: 11.17.0
- Python dependencies: project virtual environment with the dependencies declared in `pyproject.toml`
- Frontend dependencies: npm lockfile installation under `frontend/node_modules`

Evidence commands:

```text
.venv/bin/python --version
node --version
npm --version
```

## Verification baseline

### Focused backend

Command:

```text
.venv/bin/python -m pytest tests/test_api_nl2sql.py tests/test_dataset_query_service.py tests/test_run_streams.py tests/integration/test_file_datasource.py tests/integration/test_sqlite_connector.py tests/integration/test_postgres_connector.py -q -p no:cacheprovider
```

Result: PASS — 39 tests and 26 subtests passed in 3.25 seconds. One pre-existing Starlette/httpx deprecation warning was emitted.

### Frontend tests

Command: `npm --prefix frontend test`

Result: PASS — 13 test files and 54 tests passed.

### Frontend production build

Command: `npm --prefix frontend run build`

Result: PASS — TypeScript and Vite production build completed successfully; 1,592 modules were transformed.

### Full backend

Command: `.venv/bin/python -m pytest -p no:cacheprovider`

Result: PASS — 484 tests passed in 44.34 seconds. One pre-existing Starlette/httpx deprecation warning was emitted.

### Wheel

Command:

```text
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

Result: PASS — `data_agent-0.1.0-py3-none-any.whl` was built successfully (SHA-256 `ef13b5e7e860f84fccc8693a5a47b5996177647e1656c6b963aefdb11f418189`). The local pip cache was unavailable and pip continued with caching disabled.

## Baseline conclusion

All required baseline gates passed. Generated build outputs and caches created or observed during verification remain candidates only; no deletion was performed in Task 0.
