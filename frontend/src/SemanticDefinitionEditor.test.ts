import { describe, expect, it } from "vitest";

import {
  compactSemanticFieldMetadata,
  compactSemanticMetrics,
  semanticMetricError,
} from "./SemanticDefinitionEditor";

describe("semantic definition payloads", () => {
  it("removes blank optional metadata and normalizes synonyms", () => {
    expect(
      compactSemanticFieldMetadata({
        display_name: " Customer identifier ",
        description: " ",
        semantic_role: "identifier",
        unit: null,
        synonyms: [" customer key ", ""],
      }),
    ).toEqual({
      display_name: "Customer identifier",
      semantic_role: "identifier",
      synonyms: ["customer key"],
    });
  });

  it("requires governed non-count metrics to reference a bound field", () => {
    const metrics = [
      {
        metric_ref: "dataset.metrics.total_amount",
        display_name: "Total amount",
        description: "Sum of transaction amount.",
        operation: "sum" as const,
        field_ref: "dataset.Transaction.amount",
      },
    ];

    expect(
      semanticMetricError(metrics, ["dataset.Transaction.amount"]),
    ).toBeNull();
    expect(semanticMetricError(metrics, ["dataset.Transaction.id"])).toBe(
      "业务指标引用了当前绑定范围之外的逻辑字段",
    );
    expect(compactSemanticMetrics(metrics)).toEqual(metrics);
  });

  it("allows a governed row-count metric without a source field", () => {
    expect(
      semanticMetricError(
        [
          {
            metric_ref: "dataset.metrics.row_count",
            display_name: "Rows",
            description: "Rows in the governed scope.",
            operation: "count",
          },
        ],
        [],
      ),
    ).toBeNull();
  });
});
