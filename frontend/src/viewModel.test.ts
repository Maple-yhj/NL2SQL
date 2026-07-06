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
        error: "",
        trace: [{ node: "validate_sql", ok: true, message: "success" }],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.answer).toBe("East leads with 1.28M GMV.");
    expect(viewModel.rows).toEqual([{ region: "East", gmv: "1.28M" }]);
    expect(viewModel.showTable).toBe(true);
    expect(viewModel.status).toBe("validated");
    expect(viewModel.showSqlCard).toBe(false);
  });

  it("shows table data when a text response still returns multiple rows", () => {
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
        error: "",
        trace: [],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.rows).toEqual([
      { payment_installments: 1, avg_payment_amount: 104.1 },
      { payment_installments: 2, avg_payment_amount: 138.84 },
    ]);
    expect(viewModel.messageType).toBe("table");
    expect(viewModel.showTable).toBe(true);
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
        error: "",
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
