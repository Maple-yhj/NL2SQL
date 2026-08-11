import { describe, expect, it } from "vitest";

import {
  isGraphBinding,
  selectPreferredActiveBinding,
} from "./DataSourcePanel";
import type { AnySemanticBinding } from "./types";

function binding(
  bindingId: string,
  options: {
    graph?: boolean;
    status?: "draft" | "active" | "retired";
    version?: number;
    updatedAt?: string;
  } = {},
): AnySemanticBinding {
  return {
    ...(options.graph ? { schema_version: 2 } : { schema_version: 1 }),
    binding_id: bindingId,
    source_id: "source-1",
    source_snapshot_version: 1,
    schema_fingerprint: "sha256:" + "a".repeat(64),
    domain_id: `dataset.${bindingId}`,
    version: options.version ?? 1,
    status: options.status ?? "active",
    updated_at: options.updatedAt ?? "2026-08-08T00:00:00Z",
  } as AnySemanticBinding;
}

describe("datasource binding selection", () => {
  it("prefers an active v2 graph over an older active legacy binding", () => {
    const legacy = binding("legacy");
    const graph = binding("graph", { graph: true });

    expect(selectPreferredActiveBinding([legacy, graph], null)).toBe(graph);
    expect(isGraphBinding(graph)).toBe(true);
  });

  it("preserves an explicitly selected active binding and ignores retired ones", () => {
    const selected = binding("selected");
    const graph = binding("graph", { graph: true, version: 2 });
    const retiredGraph = binding("retired", {
      graph: true,
      status: "retired",
      version: 3,
    });

    expect(
      selectPreferredActiveBinding(
        [selected, graph, retiredGraph],
        selected,
      ),
    ).toBe(selected);
  });

  it("prefers the most recently activated graph when graph versions tie", () => {
    const older = binding("older", {
      graph: true,
      updatedAt: "2026-08-08T00:00:00Z",
    });
    const newer = binding("newer", {
      graph: true,
      updatedAt: "2026-08-10T00:00:00Z",
    });

    expect(selectPreferredActiveBinding([older, newer], null)).toBe(newer);
  });
});
