import { describe, expect, it } from "vitest";
import { buildEvidenceBadge, buildEvidenceRoute } from "./evidenceRoute";
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

  it("marks the terminal route complete when the backend returns real evidence", () => {
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
        evidence: [
          {
            evidence_id: "evidence-east",
            claim_key: "amount",
            artifact_id: "result-east",
            field_refs: ["region", "amount"],
          },
        ],
      },
    };

    const route = buildEvidenceRoute({
      hasBinding: true,
      runStage: "complete",
      latestAssistant,
    });

    expect(route.every((step) => step.status === "complete")).toBe(true);
  });

  it("does not claim evidence exists for a terminal answer without evidence", () => {
    const route = buildEvidenceRoute({
      hasBinding: true,
      runStage: "complete",
      latestAssistant: {
        id: "assistant-without-evidence",
        role: "assistant",
        content: "分析已完成，但没有生成证据引用。",
        metadata: {
          ok: true,
          answer: "分析已完成，但没有生成证据引用。",
          message_type: "analysis",
        },
      },
    });

    expect(route.find((step) => step.id === "evidence")).toMatchObject({
      detail: "未生成证据",
      status: "waiting",
    });
  });

  it("marks plan-only completion as not having executed data", () => {
    const route = buildEvidenceRoute({
      hasBinding: true,
      runStage: "complete",
      latestAssistant: {
        id: "assistant-plan-only",
        role: "assistant",
        content: "受治理查询计划已就绪；规划模式未执行或预览任何数据。",
        metadata: {
          ok: true,
          answer: "受治理查询计划已就绪；规划模式未执行或预览任何数据。",
          message_type: "analysis",
          sql: "SELECT order_status, COUNT(*) FROM orders GROUP BY order_status",
          artifacts: [
            {
              artifact_id: "prepared-1",
              kind: "prepared_query",
              digest: "digest-1",
              row_count: null,
              sensitivity: "metadata",
              created_at: "2026-08-10T00:00:00Z",
            },
          ],
          limitations: ["Plan mode does not execute or preview datasource rows."],
        },
      },
    });

    expect(route.find((step) => step.id === "query")?.detail).toBe("查询计划已生成");
    expect(route.find((step) => step.id === "evidence")).toMatchObject({
      detail: "规划模式未执行",
      status: "waiting",
    });
    expect(route.find((step) => step.id === "answer")?.detail).toBe("计划已生成");
  });

  it("labels a failed run with only a logical plan as having no valid evidence", () => {
    const message = {
      id: "assistant-failed-plan",
      role: "assistant",
      content: "运行未完成",
      metadata: {
        ok: false,
        message_type: "error",
        logical_plan: {},
        error: {
          code: "INTERNAL_ERROR",
          message: "failed safely",
          retryable: false,
        },
      },
    } as ChatMessage;

    expect(buildEvidenceBadge(message)).toBe("无有效证据");
  });

  it("uses actual evidence references ahead of generic route metadata", () => {
    const message = {
      id: "assistant-with-evidence",
      role: "assistant",
      content: "完成",
      metadata: {
        ok: true,
        message_type: "analysis",
        evidence: [{ evidence_id: "evidence-1" }],
      },
    } as ChatMessage;

    expect(buildEvidenceBadge(message)).toBe("1 项证据");
  });
});
