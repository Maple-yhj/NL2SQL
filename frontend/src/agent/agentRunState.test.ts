import { describe, expect, it } from "vitest";

import type { AgentEvent } from "../types";
import {
  agentRunReducer,
  createAgentRunState,
  hydrateAgentRun,
} from "./agentRunState";

const started = event({
  type: "run_started",
  sequence: 0,
  data: {
    kind: "run_started",
    mode: "execute",
    enterprise_id: "user-dataset",
    domain_id: "dataset.orders",
  },
});

const waiting = event({
  type: "run_waiting",
  sequence: 1,
  data: {
    kind: "run_waiting",
    input_request: {
      interrupt_id: "interrupt-1",
      reason: "clarification",
      prompt: "Which date range?",
      choices: ["Last 30 days"],
      allow_free_text: true,
      action_id: null,
    },
  },
});

describe("agentRunReducer", () => {
  it("rejects gaps and conflicting duplicates while accepting exact replay", () => {
    const first = agentRunReducer(createAgentRunState(), {
      type: "event",
      event: started,
    });
    expect(
      agentRunReducer(first, { type: "event", event: started }),
    ).toBe(first);
    expect(() =>
      agentRunReducer(first, {
        type: "event",
        event: {
          ...started,
          data: { ...started.data, domain_id: "other" },
        } as Extract<AgentEvent, { type: "run_started" }>,
      }),
    ).toThrow(/conflicting/i);
    expect(() =>
      agentRunReducer(first, {
        type: "event",
        event: event({
          type: "answer_synthesizing",
          sequence: 2,
          data: { kind: "answer_synthesizing", evidence_ids: [] },
        }),
      }),
    ).toThrow(/sequence/i);
  });

  it("hydrates waiting, resume and cancel states deterministically", () => {
    const hydrated = hydrateAgentRun([waiting, started]);
    expect(hydrated.status).toBe("waiting");
    expect(hydrated.inputRequest?.interrupt_id).toBe("interrupt-1");

    const resumed = agentRunReducer(hydrated, {
      type: "event",
      event: event({
        type: "run_resumed",
        sequence: 2,
        data: { kind: "run_resumed", interrupt_id: "interrupt-1" },
      }),
    });
    expect(resumed.status).toBe("running");
    const cancelled = agentRunReducer(resumed, { type: "cancelled" });
    expect(cancelled.status).toBe("cancelled");
    expect(cancelled.inputRequest).toBeNull();
  });

  it("keeps plan revisions, business tool labels and safe observations", () => {
    const events: AgentEvent[] = [
      started,
      event({
        type: "plan_updated",
        sequence: 1,
        data: {
          kind: "plan_updated",
          plan: {
            plan_id: "plan-1",
            revision: 2,
            steps: [
              {
                step_id: "step-1",
                objective: "Aggregate revenue",
                status: "pending",
                depends_on: [],
                expected_evidence: ["total"],
              },
            ],
            completion_criteria: ["Evidence-backed total"],
          },
        },
      }),
      event({
        type: "tool_started",
        sequence: 2,
        data: {
          kind: "tool_started",
          call_id: "call-1",
          action_id: "action-1",
          tool_name: "query.execute",
          display_name: "执行受治理查询",
          safe_arguments_digest: "a".repeat(64),
        },
      }),
      event({
        type: "observation_recorded",
        sequence: 3,
        data: {
          kind: "observation_recorded",
          observation_id: "observation-1",
          action_id: "action-1",
          summary: "Returned 12 grouped rows.",
          artifact_ids: ["artifact-1"],
          evidence_ids: ["evidence-1"],
        },
      }),
    ];
    const state = hydrateAgentRun(events);
    expect(state.plan?.revision).toBe(2);
    expect(state.toolCalls[0].displayName).toBe("执行受治理查询");
    expect(state.observations[0].summary).toBe("Returned 12 grouped rows.");
  });
});

function event<T extends AgentEvent>(
  value: Omit<T, "run_id" | "response"> & { response?: T["response"] },
): T {
  return {
    run_id: "run-1",
    response: null,
    ...value,
  } as T;
}
