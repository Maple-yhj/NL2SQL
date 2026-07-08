import { readFile } from "node:fs/promises";

const DEFAULT_BASE_URL = "http://127.0.0.1:5173";
const DEFAULT_EVAL_FILE = "evals/olist_questions.jsonl";
const DEFAULT_USERNAME = "yehj";
const DEFAULT_PASSWORD = "0708";

const args = parseArgs(process.argv.slice(2));
const baseUrl = (args.baseUrl || process.env.EVAL_BASE_URL || DEFAULT_BASE_URL).replace(/\/$/, "");
const evalFile = args.file || DEFAULT_EVAL_FILE;
const start = Number.parseInt(args.start || "0", 10);
const count = args.count == null ? Number.POSITIVE_INFINITY : Number.parseInt(args.count, 10);
const username = args.username || process.env.EVAL_USERNAME || DEFAULT_USERNAME;
const password = args.password || process.env.EVAL_PASSWORD || DEFAULT_PASSWORD;

const sessions = new Map();
const rows = (await readFile(evalFile, "utf8"))
  .split(/\r?\n/)
  .filter(Boolean)
  .map((line) => JSON.parse(line));

const selected = rows.slice(start, Number.isFinite(count) ? start + count : undefined);
const results = [];

for (let index = 0; index < selected.length; index += 1) {
  const row = selected[index];
  const absoluteIndex = start + index;
  const startedAt = Date.now();
  const result = await runRow(row, absoluteIndex).catch((error) => ({
    id: row.id,
    index: absoluteIndex,
    ok: false,
    rows_count: 0,
    message_type: "error",
    error: error instanceof Error ? error.message : String(error),
    sql: "",
    contextualized_question: "",
    duration_ms: Date.now() - startedAt,
  }));
  results.push(result);
  console.log(`EVAL_RESULT ${JSON.stringify(summarize(result))}`);
}

console.log(`EVAL_SUMMARY ${JSON.stringify({
  total: results.length,
  ok: results.filter((result) => result.ok).length,
  failed: results.filter((result) => !result.ok).map((result) => ({
    id: result.id,
    error: result.error,
  })),
})}`);

console.log(`EVAL_RESULTS_JSON ${JSON.stringify(results)}`);

async function runRow(row, index) {
  const tenantId = row.tenant_id || "admin";
  const startedAt = Date.now();
  const conversation = await createConversation(tenantId, `eval ${row.id}`);
  const followUp = parseFollowUp(row.question);
  if (followUp) {
    await sendMessage(tenantId, conversation.conversation_id, followUp.previous);
    const response = await sendMessage(tenantId, conversation.conversation_id, followUp.followUp);
    return normalizeResponse(row, index, response, Date.now() - startedAt);
  }

  const response = await sendMessage(tenantId, conversation.conversation_id, row.question);
  return normalizeResponse(row, index, response, Date.now() - startedAt);
}

async function login(tenantId) {
  if (sessions.has(tenantId)) {
    return sessions.get(tenantId);
  }
  const session = await jsonFetch("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      tenant_id: tenantId,
      username,
      password,
    }),
  });
  sessions.set(tenantId, session);
  return session;
}

async function createConversation(tenantId, title) {
  const session = await login(tenantId);
  return jsonFetch("/api/conversations", {
    method: "POST",
    headers: authorizationHeaders(session),
    body: JSON.stringify({ title }),
  });
}

async function sendMessage(tenantId, conversationId, question) {
  const session = await login(tenantId);
  return jsonFetch(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: "POST",
    headers: authorizationHeaders(session),
    body: JSON.stringify({
      question: question.trim(),
      execute: true,
      timeout_ms: 10_000,
      max_limit: 1_000,
      max_validation_attempts: 2,
      memory_history_limit: 8,
      include_tool_trace: true,
    }),
  });
}

async function jsonFetch(path, init = {}) {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${baseUrl}${path}`, { ...init, headers });
  const text = await response.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { raw: text };
  }
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}: ${JSON.stringify(body)}`);
  }
  return body;
}

function authorizationHeaders(session) {
  return {
    Authorization: `Bearer ${session.access_token}`,
  };
}

function normalizeResponse(row, index, response, durationMs) {
  const toolTrace = Array.isArray(response.tool_trace) ? response.tool_trace : [];
  const toolMetrics = summarizeToolTrace(toolTrace);
  return {
    id: row.id,
    index,
    question: row.question,
    tenant_id: row.tenant_id,
    type: row.type,
    ok: Boolean(response.ok),
    rows_count: Array.isArray(response.rows) ? response.rows.length : 0,
    message_type: response.message_type || "",
    error: response.error || "",
    sql: response.sql || "",
    contextualized_question: response.contextualized_question || "",
    trace: Array.isArray(response.trace) ? response.trace : [],
    tool_trace: toolTrace,
    ...toolMetrics,
    duration_ms: durationMs,
  };
}

function summarize(result) {
  return {
    id: result.id,
    ok: result.ok,
    rows_count: result.rows_count,
    message_type: result.message_type,
    error: result.error,
    duration_ms: result.duration_ms,
    tool_call_count: result.tool_call_count || 0,
    validation_retry_count: result.validation_retry_count || 0,
    tool_error_count: result.tool_error_count || 0,
    total_tool_duration_ms: result.total_tool_duration_ms || 0,
    policy_block_count: result.policy_block_count || 0,
    contextualized_question: result.contextualized_question,
    sql: result.sql.replace(/\s+/g, " ").slice(0, 240),
  };
}

function summarizeToolTrace(toolTrace) {
  const validationTools = new Set(["prepare_sql", "validate_sql"]);
  const policyErrorCodes = new Set([
    "tool_risk_not_allowed",
    "missing_tenant_id",
    "tool_call_budget_exceeded",
    "tool_write_blocked",
  ]);
  return {
    tool_call_count: toolTrace.length,
    validation_retry_count: toolTrace.filter(
      (event) => validationTools.has(event.canonical_name) && event.ok === false,
    ).length,
    tool_error_count: toolTrace.filter((event) => event.ok === false).length,
    total_tool_duration_ms: Math.round(
      toolTrace.reduce((total, event) => total + Number(event.duration_ms || 0), 0),
    ),
    policy_block_count: toolTrace.filter((event) => policyErrorCodes.has(event.error_code)).length,
  };
}

function parseFollowUp(question) {
  const marker = "。追问：";
  const prefix = "上一轮问题：";
  if (!question.startsWith(prefix) || !question.includes(marker)) {
    return null;
  }
  const withoutPrefix = question.slice(prefix.length);
  const markerIndex = withoutPrefix.indexOf(marker);
  return {
    previous: withoutPrefix.slice(0, markerIndex).trim(),
    followUp: withoutPrefix.slice(markerIndex + marker.length).trim(),
  };
}

function parseArgs(argv) {
  const parsed = {};
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (!value.startsWith("--")) {
      continue;
    }
    const key = value.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    const next = argv[index + 1];
    if (next == null || next.startsWith("--")) {
      parsed[key] = "true";
    } else {
      parsed[key] = next;
      index += 1;
    }
  }
  return parsed;
}
