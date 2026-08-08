import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const productionSources = ["types.ts", "requestPayload.ts", "api.ts", "viewModel.ts", "App.tsx"]
  .map((name) => readFileSync(new URL(`./${name}`, import.meta.url), "utf8"))
  .join("\n");

describe("Runtime product contract", () => {
  it("contains no removed request or response concepts", () => {
    for (const removed of [
      "agent_mode",
      "include_tool_trace",
      "timeout_ms",
      "max_limit",
      "max_validation_attempts",
      "memory_history_limit",
    ]) {
      expect(productionSources).not.toContain(removed);
    }
    expect(productionSources).not.toMatch(/\bintent\b/);
    expect(productionSources).not.toMatch(/\bexecute\s*:/);
  });
});
