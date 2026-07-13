import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildAgentRequest,
  loadCanonicalCases,
  normalizeResponse,
  tenantForCase,
} from "./run_olist_frontend_eval.mjs";

const bundlePath = fileURLToPath(
  new URL("../generated/bundles/olist-local.json", import.meta.url),
);

test("loads all 48 canonical evals from the compiled Domain Pack", async () => {
  const cases = await loadCanonicalCases(bundlePath);

  assert.equal(cases.length, 48);
  assert.equal(cases[0].id, "commerce.metric_001");
  assert.equal(cases.at(-1).id, "commerce.followup_003");
});

test("builds only the strict public AgentRequest fields", () => {
  assert.deepEqual(buildAgentRequest(" show gmv "), {
    question: "show gmv",
    enterprise_id: "olist",
    domain_id: "commerce",
    mode: "execute",
    requested_output: "answer",
    include_trace: true,
  });
});

test("maps canonical tenant scope to the authenticated tenant", () => {
  assert.equal(
    tenantForCase({ context: { tenantScope: "all" } }, "admin", "seller-1"),
    "admin",
  );
  assert.equal(
    tenantForCase({ context: { tenantScope: "seller" } }, "admin", "seller-1"),
    "seller-1",
  );
});

test("normalizes the current AgentResponse error and trace contracts", () => {
  const result = normalizeResponse(
    { id: "commerce.metric_001", question: "show gmv" },
    0,
    {
      ok: false,
      rows: [],
      message_type: "text",
      error: { code: "INTERNAL_ERROR", message: "safe failure", retryable: false },
      trace: [{ node: "runtime", status: "failed", error_code: "INTERNAL_ERROR" }],
      sql: null,
      contextualized_question: null,
      version_pins: null,
    },
    25,
    "admin",
  );

  assert.equal(result.error_code, "INTERNAL_ERROR");
  assert.equal(result.error, "safe failure");
  assert.deepEqual(result.trace, [
    { node: "runtime", status: "failed", error_code: "INTERNAL_ERROR" },
  ]);
  assert.equal("tool_trace" in result, false);
});
