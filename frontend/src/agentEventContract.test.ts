import { describe, expect, it } from "vitest";

import { isAgentEvent } from "./api";


describe("Agent event contract", () => {
  it("accepts the prefixed schema fingerprint emitted by context resolution", () => {
    expect(isAgentEvent({
      type: "context_resolved",
      run_id: "analysis-run-1",
      sequence: 1,
      data: {
        kind: "context_resolved",
        source_id: "source-1",
        source_version: 1,
        binding_id: "source-1-binding-1",
        binding_version: 1,
        schema_fingerprint: `sha256:${"a".repeat(64)}`,
      },
      response: null,
    })).toBe(true);

    expect(isAgentEvent({
      type: "context_resolved",
      run_id: "analysis-run-1",
      sequence: 1,
      data: {
        kind: "context_resolved",
        source_id: "source-1",
        source_version: 1,
        binding_id: "source-1-binding-1",
        binding_version: 1,
        schema_fingerprint: "sha256:not-a-digest",
      },
      response: null,
    })).toBe(false);
  });

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

    expect(isAgentEvent({
      type: "run_waiting",
      run_id: "run-1",
      sequence: 5,
      data: {
        kind: "run_waiting",
        input_request: {
          interrupt_id: "interrupt-2",
          reason: "clarification",
          origin: "dataset_query",
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
