# FastAPI Backend Implementation Plan

> **Historical / partially retained:** Retained for FastAPI contract provenance. Current runtime composition and run lifecycle are defined by the 2026-08-08 Data Analysis Agent documents.

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a FastAPI backend that exposes the existing LangGraph NL2SQL workflow through HTTP.

**Architecture:** The API layer is a thin adapter over `graph.pipeline.run_nl2sql`. Request and response validation live in `api.schemas`, route wiring lives in `api.routes`, app creation lives in `api.app`, and runtime config lives in `api.settings`.

**Tech Stack:** FastAPI, Pydantic, Uvicorn, unittest, FastAPI TestClient, existing LangGraph pipeline.

---

## Chunk 1: API Tests And Minimal Backend

### Task 1: Health Endpoint

**Files:**
- Create: `tests/test_api_health.py`
- Create: `api/app.py`
- Create: `api/routes.py`

- [ ] Write a failing test for `GET /health`.
- [ ] Run the health test and verify it fails because `api.app` is missing.
- [ ] Implement the app factory and health route.
- [ ] Run the health test and verify it passes.

### Task 2: NL2SQL Endpoint

**Files:**
- Create: `tests/test_api_nl2sql.py`
- Create: `api/schemas.py`
- Modify: `api/routes.py`

- [ ] Write failing tests for successful `/api/nl2sql`, request validation, and exception handling.
- [ ] Run the API tests and verify they fail for missing route/schema behavior.
- [ ] Implement request/response schemas and route logic.
- [ ] Run the API tests and verify they pass.

### Task 3: Packaging And Docs

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`

- [ ] Add FastAPI and Uvicorn dependencies.
- [ ] Document `uvicorn api.app:create_app --factory --reload`.
- [ ] Run the full test suite with `D:\Env\miniconda3\envs\agents-env\python.exe -m unittest discover -s tests -v`.
