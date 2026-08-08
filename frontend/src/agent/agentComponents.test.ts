import { describe, expect, it } from "vitest";

import { canSubmitAgentInput } from "./AgentInputCard";
import { toolBusinessName } from "./AgentStepItem";

describe("Agent UI helpers", () => {
  it("uses safe business labels instead of internal tool payloads", () => {
    expect(toolBusinessName("query.execute", "执行受治理查询")).toBe(
      "执行受治理查询",
    );
    expect(toolBusinessName("evidence.collect", "")).toBe("整理证据");
  });

  it("requires an allowed non-empty resume response", () => {
    expect(canSubmitAgentInput("", true)).toBe(false);
    expect(canSubmitAgentInput("Last 30 days", true)).toBe(true);
    expect(canSubmitAgentInput("Last 30 days", false)).toBe(false);
  });
});
