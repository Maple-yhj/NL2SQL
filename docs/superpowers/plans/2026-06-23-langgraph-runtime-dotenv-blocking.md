# LangGraph Runtime Dotenv Blocking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent LangGraph Studio/runtime runs from calling `load_dotenv()` inside BlockBuster-monitored async execution.

**Architecture:** Load `.env` once during project graph startup, before LangGraph background runs enter the async execution path. Runtime factories and tools read already-populated `os.environ` only.

**Tech Stack:** Python 3.12, unittest, LangGraph, python-dotenv, BlockBuster.

---

### Task 1: Runtime dotenv regression tests

**Files:**
- Modify: `tests/test_llm_protocol.py`
- Modify: `tests/test_embedding_client.py`
- Modify: `tests/test_vector_store.py`
- Modify: `tests/test_graph_tools.py`

- [ ] **Step 1: Write failing tests**

Add assertions that runtime entry points fail the test if they call `load_dotenv()`:

```python
mock.patch("core.llm.load_dotenv", side_effect=AssertionError("runtime dotenv load is blocking"))
mock.patch("core.embeddings.load_dotenv", side_effect=AssertionError("runtime dotenv load is blocking"))
mock.patch.object(vector_store, "load_dotenv", side_effect=AssertionError("runtime dotenv load is blocking"))
mock.patch.object(execute_sql_module, "load_dotenv", create=True, side_effect=AssertionError("runtime dotenv load is blocking"))
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
& 'D:\Env\miniconda3\Scripts\conda.exe' run -n agents-env python -m unittest tests.test_llm_protocol tests.test_embedding_client tests.test_vector_store tests.test_graph_tools -v
```

Expected: failures caused by the new `AssertionError`, proving the tests catch runtime dotenv loading.

### Task 2: Startup dotenv loader

**Files:**
- Create: `core/environment.py`
- Modify: `graph/pipeline.py`
- Modify: `core/llm.py`
- Modify: `core/embeddings.py`
- Modify: `rag/vector_store.py`
- Modify: `graph/tools/execute_sql.py`

- [ ] **Step 1: Add startup loader**

Create `core/environment.py`:

```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


@lru_cache(maxsize=1)
def load_project_environment() -> bool:
    project_root = Path(__file__).resolve().parents[1]
    return load_dotenv(project_root / ".env")
```

- [ ] **Step 2: Load env before graph construction**

In `graph/pipeline.py`, import and call `load_project_environment()` before importing runtime factory users:

```python
from core.environment import load_project_environment

load_project_environment()
```

- [ ] **Step 3: Remove runtime `load_dotenv()` calls**

Remove `load_dotenv()` imports and calls from:

```text
core/llm.py
core/embeddings.py
rag/vector_store.py
graph/tools/execute_sql.py
```

- [ ] **Step 4: Verify GREEN**

Run:

```powershell
& 'D:\Env\miniconda3\Scripts\conda.exe' run -n agents-env python -m unittest tests.test_llm_protocol tests.test_embedding_client tests.test_vector_store tests.test_graph_tools -v
& 'D:\Env\miniconda3\Scripts\conda.exe' run -n agents-env python -m unittest discover -s tests -v
```

Expected: focused tests and full suite pass.

### Task 3: Runtime verification

**Files:**
- No additional source edits expected.

- [ ] **Step 1: Verify BlockBuster no longer catches GraphContext construction**

Run a minimal script that imports `graph.pipeline` first, then constructs `GraphContext` inside `blockbuster_ctx()`.

Expected: script prints `ok`.

- [ ] **Step 2: Start LangGraph dev without `--allow-blocking`**

Run LangGraph dev on a non-default port and call the health endpoint and a minimal run.

Expected: startup succeeds and no `Blocking call to os.getcwd` appears for the minimal run.
