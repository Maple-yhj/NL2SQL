import { describe, expect, it } from "vitest";
import { buildEvidenceRoute } from "./evidenceRoute";
import type { ChatMessage } from "./types";

describe("buildEvidenceRoute", () => {
  it("keeps datasource setup as the first action when no binding exists", () => {
    const route = buildEvidenceRoute({
      hasBinding: false,
      runStage: "idle",
      latestAssistant: null,
    });

    expect(route.map((step) => step.status)).toEqual([
      "action",
      "waiting",
      "waiting",
      "waiting",
      "waiting",
    ]);
  });

  it("moves the active position from query to evidence during a run", () => {
    const route = buildEvidenceRoute({
      hasBinding: true,
      runStage: "pinned",
      latestAssistant: null,
    });

    expect(route.map((step) => step.status)).toEqual([
      "complete",
      "complete",
      "complete",
      "active",
      "waiting",
    ]);
  });

  it("marks the terminal route complete from a successful backend message", () => {
    const latestAssistant: ChatMessage = {
      id: "assistant-1",
      role: "assistant",
      content: "华东地区销售额最高。",
      metadata: {
        ok: true,
        answer: "华东地区销售额最高。",
        message_type: "table",
        rows: [{ region: "East", amount: 128 }],
        sql: "SELECT region, SUM(amount) FROM orders GROUP BY region",
      },
    };

    const route = buildEvidenceRoute({
      hasBinding: true,
      runStage: "complete",
      latestAssistant,
    });

    expect(route.every((step) => step.status === "complete")).toBe(true);
  });
});
