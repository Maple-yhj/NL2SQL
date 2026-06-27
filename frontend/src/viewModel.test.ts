import { describe, expect, it } from "vitest";
import { createAssistantViewModel } from "./viewModel";
import type { ChatMessage } from "./types";

describe("createAssistantViewModel", () => {
  it("keeps answer rows and status without exposing a generated SQL card", () => {
    const message: ChatMessage = {
      id: "assistant-1",
      role: "assistant",
      content: "East leads with 1.28M GMV.",
      metadata: {
        sql: "SELECT region, SUM(amount) AS gmv FROM orders",
        rows: [{ region: "East", gmv: "1.28M" }],
        answer: "East leads with 1.28M GMV.",
        ok: true,
        error: "",
        trace: [{ node: "validate_sql", ok: true, message: "success" }],
      },
    };

    const viewModel = createAssistantViewModel(message);

    expect(viewModel.answer).toBe("East leads with 1.28M GMV.");
    expect(viewModel.rows).toEqual([{ region: "East", gmv: "1.28M" }]);
    expect(viewModel.status).toBe("validated");
    expect(viewModel.showSqlCard).toBe(false);
  });
});
