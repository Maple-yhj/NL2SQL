# NL2SQL MVP Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist query result rows into conversation history, then build a React + Vite MVP matching `canva-nl2sql-style2-user-entry.html`.

**Architecture:** Keep FastAPI as the API backend and add a separate `frontend/` Vite SPA. The frontend authenticates with JWT, uses conversation endpoints as the primary NL2SQL flow, and renders assistant answers with a Rows card only.

**Tech Stack:** FastAPI, unittest, React, Vite, TypeScript, CSS, browser Fetch API.

---

### Task 1: Persist Rows In Conversation History

**Files:**
- Modify: `tests/test_api_conversations.py`
- Modify: `tests/test_memory_store.py`
- Modify: `graph/memory_store.py`
- Modify: `graph/node.py`

- [ ] **Step 1: Write failing tests**

Add assertions that assistant message metadata contains the same rows returned by the NL2SQL execution:

```python
self.assertEqual(
    response.json()["items"][1]["metadata"]["rows"],
    [{"region": "East", "gmv": "1.28M"}],
)
```

Add a memory-store unit test that calls `save_turn(..., rows=[{"region": "East"}])` and verifies `list_messages()` returns that metadata.

- [ ] **Step 2: Run tests to verify RED**

Run:

```powershell
conda run -n agents-env python -m unittest tests.test_memory_store tests.test_api_conversations -v
```

Expected: failure because `save_turn()` does not accept or persist `rows`.

- [ ] **Step 3: Implement minimal backend change**

Update `ConversationStoreProtocol.save_turn`, `NullConversationStore.save_turn`, `InMemoryConversationStore.save_turn`, and `PostgresConversationStore.save_turn` to accept `rows: list[dict[str, Any]]`. Store assistant metadata as:

```python
{
    "sql": sql,
    "rows": deepcopy(rows),
    "answer": answer,
    "ok": ok,
    "error": error,
    "trace": deepcopy(trace),
}
```

Update `persist_memory_node()` to pass `rows=state.get("rows", [])`.

- [ ] **Step 4: Run tests to verify GREEN**

Run the same unittest command. Expected: all selected tests pass.

### Task 2: Scaffold React + Vite Frontend

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.node.json`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/api.ts`
- Create: `frontend/src/types.ts`
- Create: `frontend/src/styles.css`

- [ ] **Step 1: Create project files**

Use Vite with `@vitejs/plugin-react`. Configure dev proxy:

```ts
server: {
  proxy: {
    "/api": "http://127.0.0.1:8000",
    "/health": "http://127.0.0.1:8000"
  }
}
```

- [ ] **Step 2: Implement API client**

Create a small typed Fetch client with:

```ts
login(payload)
refresh(refreshToken)
me()
logout(refreshToken)
health()
listConversations()
createConversation(title)
listMessages(conversationId)
sendMessage(conversationId, payload)
```

On 401 for protected calls, try one refresh once, then clear session and return to login.

- [ ] **Step 3: Implement UI**

Build a login view and a workspace view:

```text
left rail: brand, new chat, conversation list, session context
top: title, health pill, execute toggle, user menu
center: chat messages
bottom: composer, memory 8, max_limit 1000, send button
```

Assistant messages render Answer text and Rows table only. Do not render Generated SQL.

- [ ] **Step 4: Build and run**

Run:

```powershell
cd frontend
npm install
npm run build
npm run dev -- --host 127.0.0.1
```

Expected: build succeeds and Vite prints a local URL.

### Task 3: Verify MVP

**Files:**
- Read: `frontend/dist`
- Read: browser page at Vite URL

- [ ] **Step 1: Backend verification**

Run:

```powershell
conda run -n agents-env python -m unittest tests.test_memory_store tests.test_api_conversations tests.test_api_nl2sql tests.test_api_auth -v
```

Expected: selected API/auth/conversation tests pass.

- [ ] **Step 2: Frontend verification**

Run:

```powershell
cd frontend
npm run build
```

Expected: TypeScript and Vite build complete without errors.

- [ ] **Step 3: Browser verification**

Open the Vite URL. Verify the app loads, shows login when unauthenticated, and the workspace layout matches the approved HTML prototype after authentication.
