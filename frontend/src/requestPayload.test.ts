import { describe, expect, it } from "vitest";
import { createSendMessagePayload } from "./requestPayload";

describe("createSendMessagePayload", () => {
  it("emits only the strict Runtime request contract for the selected mode", () => {
    expect(createSendMessagePayload(" show gmv ", "plan")).toEqual({
      question: "show gmv",
      enterprise_id: "olist",
      domain_id: "commerce",
      mode: "plan",
      requested_output: "answer",
      include_trace: false,
    });
  });
});
