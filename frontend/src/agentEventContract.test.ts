import { describe, expect, it } from "vitest";

import { isAgentEvent } from "./api";


describe("Agent event contract", () => {
  it("accepts a strict waiting event without a terminal response", () => {
    expect(isAgentEvent({
      type: "run_waiting",
      run_id: "run-1",
      sequence: 4,
      data: {
        kind: "run_waiting",
        input_request: {
          interrupt_id: "interrupt-1",
          reason: "clarification",
          prompt: "Which date field?",
          choices: [],
          allow_free_text: true,
          action_id: null,
        },
      },
      response: null,
    })).toBe(true);
  });

  it("rejects mismatched and untyped event payloads", () => {
    expect(isAgentEvent({
      type: "run_waiting",
      run_id: "run-1",
      sequence: 4,
      data: { kind: "tool_started" },
      response: null,
    })).toBe(false);
    expect(isAgentEvent({
      type: "tool_started",
      run_id: "run-1",
      sequence: 3,
      data: {
        kind: "tool_started",
        call_id: "call-1",
        action_id: "action-1",
        tool_name: "query.execute",
        display_name: "Execute query",
        safe_arguments_digest: "a".repeat(64),
        raw_sql: "select secret",
      },
      response: null,
    })).toBe(false);
  });
});
