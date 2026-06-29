import { describe, expect, it } from "vitest";
import { createSendMessagePayload } from "./requestPayload";

describe("createSendMessagePayload", () => {
  it("uses hidden defaults and always executes the generated SQL", () => {
    expect(createSendMessagePayload(" show gmv ")).toEqual({
      question: "show gmv",
      execute: true,
      timeout_ms: 10_000,
      max_limit: 1_000,
      max_validation_attempts: 2,
      memory_history_limit: 8,
    });
  });
});
