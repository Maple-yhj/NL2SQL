import { describe, expect, it } from "vitest";
import {
  createAssistantViewModel,
  formatCellValue,
  formatCellValueForColumn,
  formatColumnLabel,
  getColumnClassName,
} from "./viewModel";
import type { ChatMessage } from "./types";

describe("createAssistantViewModel", () => {
  it("shows rows only when the backend marks the message as a table", () => {
    const message: ChatMessage = {
      id: "assistant-1",
      role: "assistant",
      content: "East leads with 1.28M GMV.",
      metadata: {
        sql: "SELECT region, SUM(amount) AS gmv FROM orders",
        rows: [{ region: "East", gmv: "1.28M" }],
        answer: "East leads with 1.28M GMV.",
        message_type: "table",
        ok: true,
        error: null,
        trace: [{ node: "validate_sql", status: "completed", error_code: null }],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.answer).toBe("East leads with 1.28M GMV.");
    expect(viewModel.rows).toEqual([{ region: "East", gmv: "1.28M" }]);
    expect(viewModel.showTable).toBe(true);
    expect(viewModel.status).toBe("validated");
    expect(viewModel.showSqlCard).toBe(true);
    expect(viewModel.sql).toBe("SELECT region, SUM(amount) AS gmv FROM orders");
  });

  it("preserves an explicit text response even when rows are present", () => {
    const message: ChatMessage = {
      id: "assistant-2",
      role: "assistant",
      content: "Average payment amount by credit-card installments.",
      metadata: {
        rows: [
          { payment_installments: 1, avg_payment_amount: 104.1 },
          { payment_installments: 2, avg_payment_amount: 138.84 },
        ],
        answer: "Average payment amount by credit-card installments.",
        message_type: "text",
        ok: true,
        error: null,
        trace: [],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.rows).toEqual([
      { payment_installments: 1, avg_payment_amount: 104.1 },
      { payment_installments: 2, avg_payment_amount: 138.84 },
    ]);
    expect(viewModel.messageType).toBe("text");
    expect(viewModel.showTable).toBe(false);
  });

  it("preserves an explicit server message type on a failed response", () => {
    const message: ChatMessage = {
      id: "assistant-chart-error",
      role: "assistant",
      content: "The governed run failed safely.",
      metadata: {
        message_type: "chart",
        ok: false,
        error: {
          code: "INTERNAL_ERROR",
          message: "The governed run failed safely.",
          retryable: false,
        },
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.messageType).toBe("chart");
    expect(viewModel.status).toBe("error");
  });

  it("falls back to error only when the server message type is absent", () => {
    const message: ChatMessage = {
      id: "assistant-untyped-error",
      role: "assistant",
      content: "The governed run failed safely.",
      metadata: {
        ok: false,
        error: {
          code: "INTERNAL_ERROR",
          message: "The governed run failed safely.",
          retryable: false,
        },
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.messageType).toBe("error");
    expect(viewModel.status).toBe("error");
  });

  it("removes markdown table text when structured table rows are available", () => {
    const message: ChatMessage = {
      id: "assistant-with-markdown-table",
      role: "assistant",
      content: "unused",
      metadata: {
        rows: [
          { customer_state: "SP", customer_city: "sao paulo", customer_count: 15540 },
          { customer_state: "RJ", customer_city: "rio de janeiro", customer_count: 6882 },
        ],
        answer: [
          "根据查询结果，客户最多的州是 **SP**。",
          "",
          "| State | City | Customer Count |",
          "|-------|------|----------------|",
          "| SP | sao paulo | 15,540 |",
          "| RJ | rio de janeiro | 6,882 |",
        ].join("\n"),
        message_type: "table",
        ok: true,
        error: null,
        trace: [],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.showTable).toBe(true);
    expect(viewModel.answer).toBe("根据查询结果，客户最多的州是 **SP**。");
    expect(viewModel.answer).not.toContain("| State |");
    expect(viewModel.answer).not.toContain("sao paulo | 15,540");
  });

  it("removes false preview limit claims when structured rows are available", () => {
    const message: ChatMessage = {
      id: "assistant-with-preview-claim",
      role: "assistant",
      content: "unused",
      metadata: {
        rows: [
          { customer_state: "SP", customer_city: "sao paulo", customer_count: 15540 },
          { customer_state: "RJ", customer_city: "rio de janeiro", customer_count: 6882 },
        ],
        answer:
          "Only the first 10 rows are shown. Run the original query to get all 20 rows. Sao Paulo leads clearly.",
        message_type: "table",
        ok: true,
        error: null,
        trace: [],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.showTable).toBe(true);
    expect(viewModel.answer).toBe("Sao Paulo leads clearly.");
    expect(viewModel.answer).not.toContain("first 10 rows");
    expect(viewModel.answer).not.toContain("original query");
  });

  it("removes row-by-row bullet descriptions when structured rows are available", () => {
    const message: ChatMessage = {
      id: "assistant-with-row-list",
      role: "assistant",
      content: "unused",
      metadata: {
        rows: [
          { seller_state: "AC", avg_carrier_handoff_duration: null },
          { seller_state: "AM", avg_carrier_handoff_duration: "P3DT10H19M12.333333S" },
          { seller_state: "BA", avg_carrier_handoff_duration: "P3DT9H30M16.553259S" },
        ],
        answer: [
          "\u6839\u636e\u67e5\u8be2\u7ed3\u679c\uff0c\u6309\u5356\u5bb6\u5dde\u7edf\u8ba1\u7684\u5e73\u5747\u4ece\u8d2d\u4e70\u5230\u4ea4\u7ed9\u627f\u8fd0\u5546\u7684\u65f6\u95f4\u5982\u4e0b\uff1a",
          "",
          "- AC: \u65e0\u6570\u636e (null)",
          "- AM: 3\u592910\u5c0f\u65f619\u520612\u79d2",
          "- BA: 3\u59299\u5c0f\u65f630\u520616\u79d2",
          "\uff08\u5171\u8fd4\u56de23\u6761\u8bb0\u5f55\uff0c\u4ec5\u5217\u51fa\u63d0\u4f9b\u768410\u6761\u3002\u5176\u4f59\u5dde\u7684\u5e73\u5747\u65f6\u957f\u672a\u5728\u7ed3\u679c\u4e2d\u5c55\u793a\u3002\uff09",
        ].join("\n"),
        message_type: "table",
        ok: true,
        error: null,
        trace: [],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.showTable).toBe(true);
    expect(viewModel.answer).toBe("");
  });

  it("restores table display for legacy assistant history with detail rows but no message type", () => {
    const message: ChatMessage = {
      id: "assistant-legacy",
      role: "assistant",
      content: "Returned the latest order records.",
      metadata: {
        rows: [
          { id: 10230, region: "East", amount: 1751.64 },
          { id: 17503, region: "East", amount: 747.77 },
        ],
        answer: "Returned the latest order records.",
        ok: true,
        error: null,
        trace: [],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.messageType).toBe("table");
    expect(viewModel.showTable).toBe(true);
  });

  it("marks local thinking messages as pending", () => {
    const message: ChatMessage = {
      id: "assistant-thinking",
      role: "assistant",
      content: "",
      metadata: {
        message_type: "thinking",
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.isThinking).toBe(true);
    expect(viewModel.status).toBe("pending");
    expect(viewModel.showTable).toBe(false);
  });

  it("exposes plan, structured error, and pending memory proposals", () => {
    const message: ChatMessage = {
      id: "assistant-plan",
      role: "assistant",
      content: "",
      metadata: {
        logical_plan: { analysis_type: "aggregate", metrics: ["commerce.gmv"] },
        sql: "SELECT 1",
        error: {
          code: "COST_EXCEEDED",
          message: "The governed run failed safely.",
          retryable: false,
        },
        pending_memory_updates: [
          { scope: "user", source: "runtime.finalize", status: "pending_approval" },
        ],
        ok: false,
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.logicalPlan).toEqual(message.metadata.logical_plan);
    expect(viewModel.sql).toBe("SELECT 1");
    expect(viewModel.error?.code).toBe("COST_EXCEEDED");
    expect(viewModel.pendingMemoryUpdates).toHaveLength(1);
    expect(viewModel.status).toBe("error");
  });

  it("uses Chinese labels for common table columns", () => {
    expect(formatColumnLabel("id")).toBe("订单ID");
    expect(formatColumnLabel("region")).toBe("地区");
    expect(formatColumnLabel("amount")).toBe("订单金额");
    expect(formatColumnLabel("created_at")).toBe("创建时间");
    expect(formatColumnLabel("product_id")).toBe("产品ID");
    expect(formatColumnLabel("user_id")).toBe("用户ID");
    expect(formatColumnLabel("order_status")).toBe("Order Status");
  });

  it("formats table cell values by column meaning", () => {
    expect(formatCellValueForColumn("2024-12-30T23:45:00Z", "created_at")).toBe(
      "2024-12-30 23:45",
    );
    expect(
      formatCellValueForColumn("P3DT10H19M12.333333S", "avg_carrier_handoff_duration"),
    ).toBe("3\u592910\u5c0f\u65f619\u5206\u949f");
    expect(formatCellValueForColumn("PT45S", "avg_duration")).toBe("\u4e0d\u8db31\u5206\u949f");
    expect(formatCellValueForColumn(1751.6, "amount")).toBe("1751.60");
    expect(formatCellValueForColumn(2, "quantity")).toBe("2");
    expect(formatCellValue(["a", "b"])).toBe("[\"a\",\"b\"]");
  });

  it("marks numeric table columns for right alignment", () => {
    expect(getColumnClassName("amount")).toBe("numeric-cell");
    expect(getColumnClassName("quantity")).toBe("numeric-cell");
    expect(getColumnClassName("region")).toBe("");
  });
});
