import { describe, expect, it } from "vitest";
import { createSendMessagePayload } from "./requestPayload";

describe("createSendMessagePayload", () => {
  it("pins an activated user datasource and binding as one request scope", () => {
    expect(
      createSendMessagePayload(
        " top orders ",
        {
          binding_id: "orders-binding-1",
          tenant_id: "tenant-a",
          source_id: "orders",
          source_snapshot_version: 2,
          domain_id: "dataset.orders",
          version: 3,
          status: "active",
          mappings: [],
          created_at: "2026-07-26T00:00:00Z",
          updated_at: "2026-07-26T00:00:00Z",
        },
        "execute",
      ),
    ).toEqual({
      question: "top orders",
      enterprise_id: "user-dataset",
      domain_id: "dataset.orders",
      source_id: "orders",
      source_version: 2,
      binding_id: "orders-binding-1",
      binding_version: 3,
      mode: "execute",
      requested_output: "answer",
      include_trace: false,
    });
  });
});
