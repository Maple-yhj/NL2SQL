# Plan DSL Planner Layer Implementation Plan

> **Historical / superseded:** Retained for deterministic compilation and SQL-safety decisions. The fixed Pack planner was replaced by the native dataset Analysis Agent and dataset-query contracts.

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Plan DSL planner layer between intent parsing and SQL generation while keeping all existing public APIs and SQL safety tools compatible.

**Architecture:** The current LangGraph remains a fixed DAG. A new `plan_query` node converts `QueryIntent` into a serializable Plan DSL and a fixed-DAG projection of an Execution Graph. Downstream nodes consume the plan as orchestration context while still calling the existing schema search, SQL generator, validator, and executor.

**Tech Stack:** Python dataclasses, LangGraph state, existing LLM protocol, existing `extract_json_object`, `unittest`.

---

## File Structure

- Create `engine/plan_models.py`: Plan DSL dataclasses, dict parsing, fallback construction from `QueryIntent`, conversion back to `QueryIntent`, execution graph projection, prompt formatting.
- Create `engine/planner.py`: LLM planner wrapper with deterministic fallback to Plan DSL when model output is invalid.
- Modify `graph/state.py`: add internal-only plan fields; keep `InputState` and `OutputState` unchanged.
- Modify `graph/node.py`: add `plan_query_node`, use plan-derived query and intent in metric/schema/generator nodes.
- Modify `graph/pipeline.py`: insert `plan_query` after `parse_intent`.
- Modify `graph/tools/sql_generator.py`: add optional `plan_context` to prompt builder and generator.
- Add tests in `tests/test_plan_models.py`, `tests/test_planner.py`, and update graph/generator tests.

## Chunk 1: Plan DSL Models

- [x] Write failing tests for Plan DSL classification, conversion, and execution graph projection.
- [x] Implement `engine/plan_models.py` with stable serializable dataclasses.
- [x] Verify focused model tests pass.

## Chunk 2: Planner Node

- [x] Write failing tests for planner fallback and graph node behavior.
- [x] Implement `engine/planner.py`.
- [x] Add `plan_query_node` and new state fields.
- [x] Verify focused planner and node tests pass.

## Chunk 3: Pipeline and Generator Integration

- [x] Write failing tests for pipeline trace and SQL generator plan prompt.
- [x] Insert `plan_query` in `graph/pipeline.py`.
- [x] Pass plan context to the existing SQL generator without breaking old calls.
- [x] Verify focused pipeline and generator tests pass.

## Chunk 4: Regression Verification

- [x] Run targeted graph and planner test suite.
- [x] Run broader backend unit tests if available in the active Python environment.
- [x] Report exact verification commands and any remaining gaps.
