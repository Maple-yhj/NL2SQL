# LangSmith Studio GraphContext Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the compiled NL2SQL graph constructible from LangSmith Studio runtime context without exposing Python service objects in JSON Schema or rejecting Studio-owned metadata such as `thread_id`.

**Architecture:** Replace the frozen dataclass context with a frozen Pydantic model. Keep scalar runtime settings in the generated context schema, exclude LLM and embedding clients from schema/serialization, construct those clients through patchable default factories, and ignore unknown Studio metadata while preserving explicit dependency injection.

**Tech Stack:** Python 3.12, Pydantic 2, LangGraph 1.2, `unittest`

---

## File Structure

- Create `tests/test_graph_context.py`: focused regression tests for schema generation, Studio metadata handling, default client construction, and explicit dependency injection.
- Modify `graph/context.py`: Pydantic runtime-context model and client factories; no graph-node changes.

### Task 1: Add GraphContext regression tests

**Files:**
- Create: `tests/test_graph_context.py`
- Test: `tests/test_graph_context.py`

- [ ] **Step 1: Write the failing regression tests**

```python
import unittest
from unittest import mock

from graph.context import GraphContext
from graph.pipeline import graph


class FakeLLM:
    async def complete(self, prompt, system="", max_output_tokens=2048):
        return "unused"


class FakeEmbeddings:
    model_name = "fake"
    dimension = 3

    async def embed_text(self, text):
        return [0.1, 0.2, 0.3]

    async def embed_texts(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class GraphContextTests(unittest.TestCase):
    def test_context_schema_exposes_only_serializable_settings(self):
        try:
            schema = graph.get_context_jsonschema()
        except Exception as exc:
            self.fail(f"Context schema must be JSON serializable: {exc}")

        properties = schema["properties"]
        self.assertNotIn("llm", properties)
        self.assertNotIn("embeddings", properties)
        self.assertEqual(
            set(properties),
            {"dsn", "timeout_ms", "max_limit", "max_validation_attempts"},
        )

    def test_context_ignores_studio_thread_id(self):
        llm = FakeLLM()
        embeddings = FakeEmbeddings()
        try:
            context = GraphContext(
                llm=llm,
                embeddings=embeddings,
                thread_id="studio-thread",
            )
        except TypeError as exc:
            self.fail(f"Studio metadata must not break GraphContext: {exc}")

        self.assertIs(context.llm, llm)
        self.assertIs(context.embeddings, embeddings)

    def test_context_builds_default_runtime_clients(self):
        llm = FakeLLM()
        embeddings = FakeEmbeddings()
        with (
            mock.patch("graph.context.create_llm", return_value=llm, create=True),
            mock.patch(
                "graph.context.create_embedding_client",
                return_value=embeddings,
                create=True,
            ),
        ):
            try:
                context = GraphContext()
            except TypeError as exc:
                self.fail(f"GraphContext must provide runtime defaults: {exc}")

        self.assertIs(context.llm, llm)
        self.assertIs(context.embeddings, embeddings)

    def test_context_preserves_explicit_dependency_injection(self):
        llm = FakeLLM()
        embeddings = FakeEmbeddings()
        context = GraphContext(llm=llm, embeddings=embeddings)

        self.assertIs(context.llm, llm)
        self.assertIs(context.embeddings, embeddings)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
conda run -n agents-env python -m unittest tests.test_graph_context -v
```

Expected: three failures caused by the current dataclass: JSON Schema generation cannot handle `LLMProtocol`, `thread_id` is rejected, and LLM/embedding defaults are missing. The explicit dependency-injection test passes.

- [ ] **Step 3: Commit the failing tests**

```powershell
git add tests/test_graph_context.py
git commit -m "test: reproduce LangSmith graph context failure"
```

### Task 2: Make GraphContext Studio-compatible

**Files:**
- Modify: `graph/context.py`
- Test: `tests/test_graph_context.py`
- Test: `tests/test_graph_nodes.py`

- [ ] **Step 1: Replace the dataclass with the minimal Pydantic model**

Replace `graph/context.py` with:

```python
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from core.embeddings import (
    EmbeddingClientProtocol,
    create_embedding_client,
)
from core.llm import LLMProtocol, create_llm


class GraphContext(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="ignore",
        frozen=True,
    )

    llm: SkipJsonSchema[LLMProtocol] = Field(
        default_factory=lambda: create_llm(),
        exclude=True,
        repr=False,
    )
    embeddings: SkipJsonSchema[EmbeddingClientProtocol] = Field(
        default_factory=lambda: create_embedding_client(),
        exclude=True,
        repr=False,
    )
    dsn: str | None = None
    timeout_ms: int = 10_000
    max_limit: int = 1000
    max_validation_attempts: int = 2
```

- [ ] **Step 2: Run the focused context tests and verify GREEN**

Run:

```powershell
conda run -n agents-env python -m unittest tests.test_graph_context -v
```

Expected: `Ran 4 tests` and `OK`.

- [ ] **Step 3: Run existing graph-node dependency-injection tests**

Run:

```powershell
conda run -n agents-env python -m unittest tests.test_graph_nodes -v
```

Expected: all existing graph-node tests pass, proving `runtime.context.<field>` and explicit fake dependencies remain compatible.

- [ ] **Step 4: Commit the production fix**

```powershell
git add graph/context.py
git commit -m "fix: make graph context compatible with LangSmith Studio"
```

### Task 3: Verify the complete project

**Files:**
- Verify: `graph/context.py`
- Verify: `tests/test_graph_context.py`
- Verify: `tests/`

- [ ] **Step 1: Verify the generated context schema directly**

Run:

```powershell
conda run -n agents-env python -c "from graph.pipeline import graph; s=graph.get_context_jsonschema(); print(sorted(s['properties'])); assert 'llm' not in s['properties']; assert 'embeddings' not in s['properties']"
```

Expected output:

```text
['dsn', 'max_limit', 'max_validation_attempts', 'timeout_ms']
```

- [ ] **Step 2: Run the full unit test suite**

Run:

```powershell
conda run -n agents-env python -m unittest discover -s tests -v
```

Expected: all tests pass with no errors or failures.

- [ ] **Step 3: Start LangGraph development server**

Run:

```powershell
conda activate agents-env
langgraph dev --no-browser
```

Expected: server starts at `http://127.0.0.1:2024` without `PydanticSchemaGenerationError` or `GraphContext.__init__()` errors. A subsequent Studio run may fail only for real provider/database configuration, not for `thread_id` context construction.

- [ ] **Step 4: Review final scope**

Run:

```powershell
git status --short
git diff HEAD~2 -- graph/context.py tests/test_graph_context.py
```

Expected: implementation changes are limited to `graph/context.py` and `tests/test_graph_context.py`; pre-existing unrelated workspace changes remain untouched.
