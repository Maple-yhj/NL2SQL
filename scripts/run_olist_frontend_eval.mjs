import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULT_BASE_URL = "http://127.0.0.1:5173";
const DEFAULT_BUNDLE_FILE = "generated/bundles/olist-local.json";
const DEFAULT_ADMIN_TENANT_ID = "admin";
const DEFAULT_SELLER_TENANT_ID = "3442f8959a84dea7ee197c632cb2df15";

export async function loadCanonicalCases(bundleFile = DEFAULT_BUNDLE_FILE) {
  const document = JSON.parse(await readFile(bundleFile, "utf8"));
  const cases = document?.bundle?.semantic_model?.evals;
  if (!Array.isArray(cases) || cases.length !== 48) {
    throw new Error("compiled OList Domain Pack must contain exactly 48 eval cases");
  }
  const ids = new Set();
  for (const item of cases) {
    if (
      item === null ||
      typeof item !== "object" ||
      typeof item.id !== "string" ||
      typeof item.question !== "string" ||
      ids.has(item.id)
    ) {
      throw new Error("compiled OList Domain Pack contains an invalid eval case");
    }
    ids.add(item.id);
  }
  return cases;
}

export function buildAgentRequest(question) {
  const normalized = question.trim();
  if (!normalized) {
    throw new Error("eval question must not be blank");
  }
  return {
    question: normalized,
    enterprise_id: "olist",
    domain_id: "commerce",
    mode: "execute",
    requested_output: "answer",
    include_trace: true,
  };
}

export function tenantForCase(row, adminTenantId, sellerTenantId) {
  const scope = row?.context?.tenantScope;
  if (scope === "all") {
    return adminTenantId;
  }
  if (scope === "seller") {
    return sellerTenantId;
  }
  throw new Error(`unsupported eval tenant scope: ${String(scope)}`);
}

export function normalizeResponse(row, index, response, durationMs, tenantId) {
  const trace = Array.isArray(response.trace) ? response.trace : [];
  const error = isRecord(response.error) ? response.error : null;
  return {
    id: row.id,
    index,
    question: row.question,
    tenant_id: tenantId,
    tenant_scope: row.context?.tenantScope ?? "",
    analysis_type: row.analysisType ?? "",
    ok: response.ok === true,
    rows_count: Array.isArray(response.rows) ? response.rows.length : 0,
    message_type: typeof response.message_type === "string" ? response.message_type : "",
    error_code: typeof error?.code === "string" ? error.code : "",
    error: typeof error?.message === "string" ? error.message : "",
    sql: typeof response.sql === "string" ? response.sql : "",
    contextualized_question:
      typeof response.contextualized_question === "string"
        ? response.contextualized_question
        : "",
    trace,
    version_pins: isRecord(response.version_pins) ? response.version_pins : null,
    duration_ms: durationMs,
  };
}

export async function main(argv = process.argv.slice(2), environment = process.env) {
  const args = parseArgs(argv);
  const baseUrl = (args.baseUrl || environment.EVAL_BASE_URL || DEFAULT_BASE_URL).replace(
    /\/$/,
    "",
  );
  const bundleFile = args.bundle || environment.EVAL_BUNDLE_FILE || DEFAULT_BUNDLE_FILE;
  const start = parseNonNegativeInteger(args.start, 0, "start");
  const count =
    args.count == null
      ? Number.POSITIVE_INFINITY
      : parseNonNegativeInteger(args.count, undefined, "count");
  const username = args.username || environment.EVAL_USERNAME;
  const password = args.password || environment.EVAL_PASSWORD;
  if (!username || !password) {
    throw new Error("EVAL_USERNAME and EVAL_PASSWORD (or CLI equivalents) are required");
  }
  const adminTenantId =
    args.adminTenantId || environment.EVAL_ADMIN_TENANT_ID || DEFAULT_ADMIN_TENANT_ID;
  const sellerTenantId =
    args.sellerTenantId ||
    environment.EVAL_SELLER_TENANT_ID ||
    DEFAULT_SELLER_TENANT_ID;

  const rows = await loadCanonicalCases(bundleFile);
  const selected = rows.slice(
    start,
    Number.isFinite(count) ? start + count : undefined,
  );
  const sessions = new Map();
  const results = [];

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

  async function login(tenantId) {
    if (sessions.has(tenantId)) {
      return sessions.get(tenantId);
    }
    const session = await jsonFetch("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ tenant_id: tenantId, username, password }),
    });
    sessions.set(tenantId, session);
    return session;
  }

  function authorizationHeaders(session) {
    return { Authorization: `Bearer ${session.access_token}` };
  }

  async function createConversation(tenantId, title) {
    const session = await login(tenantId);
    return jsonFetch("/api/conversations", {
      method: "POST",
      headers: authorizationHeaders(session),
      body: JSON.stringify({ title, domain_id: "commerce" }),
    });
  }

  async function sendMessage(tenantId, conversationId, question) {
    const session = await login(tenantId);
    return jsonFetch(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: "POST",
        headers: authorizationHeaders(session),
        body: JSON.stringify(buildAgentRequest(question)),
      },
    );
  }

  async function runRow(row, index) {
    const tenantId = tenantForCase(row, adminTenantId, sellerTenantId);
    const startedAt = Date.now();
    const conversation = await createConversation(tenantId, `eval ${row.id}`);
    const followUp = parseFollowUp(row.question);
    if (followUp) {
      await sendMessage(tenantId, conversation.conversation_id, followUp.previous);
      const response = await sendMessage(
        tenantId,
        conversation.conversation_id,
        followUp.followUp,
      );
      return normalizeResponse(
        row,
        index,
        response,
        Date.now() - startedAt,
        tenantId,
      );
    }
    const response = await sendMessage(
      tenantId,
      conversation.conversation_id,
      row.question,
    );
    return normalizeResponse(
      row,
      index,
      response,
      Date.now() - startedAt,
      tenantId,
    );
  }

  for (let index = 0; index < selected.length; index += 1) {
    const row = selected[index];
    const absoluteIndex = start + index;
    const startedAt = Date.now();
    const tenantId = tenantForCase(row, adminTenantId, sellerTenantId);
    const result = await runRow(row, absoluteIndex).catch((error) => ({
      id: row.id,
      index: absoluteIndex,
      question: row.question,
      tenant_id: tenantId,
      tenant_scope: row.context?.tenantScope ?? "",
      analysis_type: row.analysisType ?? "",
      ok: false,
      rows_count: 0,
      message_type: "error",
      error_code: "EVAL_RUNNER_ERROR",
      error: error instanceof Error ? error.message : String(error),
      sql: "",
      contextualized_question: "",
      trace: [],
      version_pins: null,
      duration_ms: Date.now() - startedAt,
    }));
    results.push(result);
    console.log(`EVAL_RESULT ${JSON.stringify(summarize(result))}`);
  }

  console.log(
    `EVAL_SUMMARY ${JSON.stringify({
      total: results.length,
      ok: results.filter((result) => result.ok).length,
      failed: results
        .filter((result) => !result.ok)
        .map((result) => ({
          id: result.id,
          error_code: result.error_code,
          error: result.error,
        })),
    })}`,
  );
  console.log(`EVAL_RESULTS_JSON ${JSON.stringify(results)}`);
  return results;
}

function summarize(result) {
  return {
    id: result.id,
    ok: result.ok,
    rows_count: result.rows_count,
    message_type: result.message_type,
    error_code: result.error_code,
    error: result.error,
    duration_ms: result.duration_ms,
    trace_error_count: result.trace.filter((event) => event?.status === "failed").length,
    contextualized_question: result.contextualized_question,
    sql: result.sql.replace(/\s+/g, " ").slice(0, 240),
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

function parseNonNegativeInteger(value, fallback, name) {
  if (value == null) {
    return fallback;
  }
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new Error(`${name} must be a non-negative integer`);
  }
  return parsed;
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

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const entryUrl = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : "";
if (import.meta.url === entryUrl) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
