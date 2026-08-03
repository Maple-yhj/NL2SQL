import { describe, expect, it } from "vitest";
import { graphEditorReducer } from "./relationshipGraphState";
import type { RelationshipGraphDraft } from "../types";

const graph = { graph_id: "g", tenant_id: "t", source_id: "s", source_snapshot_version: 1, schema_fingerprint: "sha256:x", revision: 1, status: "draft", nodes: [], edges: [], components: [], route_rules: [] } as RelationshipGraphDraft;
const edge = { edge_id: "e", from_node_id: "a", to_node_id: "b", conditions: [{ from_column_id: "x", operator: "eq", to_column_id: "y" }], cardinality: "unknown", join_semantics: "inner", preserve_node_id: null, route_priority: 100, enabled: true, provenance: { source: "user" }, quality: null } as import("../types").RelationshipEdge;
describe("graphEditorReducer", () => {
  it("supports local edge edits and undo", () => {
    const added = graphEditorReducer({ graph, undo: [], redo: [] }, { type: "addEdge", edge });
    expect(added.graph?.edges).toHaveLength(1);
    expect(graphEditorReducer(added, { type: "undo" }).graph?.edges).toHaveLength(0);
  });
  it("removes affected edges with a removed node", () => {
    const withNode = { ...graph, nodes: [{ node_id: "a", relation_id: "r", role_name: "a", logical_entity: "A", enabled: true }], edges: [edge] };
    const removed = graphEditorReducer({ graph: withNode, undo: [], redo: [] }, { type: "removeNode", nodeId: "a" });
    expect(removed.graph?.nodes).toHaveLength(0);
    expect(removed.graph?.edges).toHaveLength(0);
  });
  it("keeps recommendation acceptance edits in undo history", () => {
    const added = graphEditorReducer({ graph, undo: [], redo: [] }, { type: "addEdge", edge });
    const edited = graphEditorReducer(added, { type: "updateEdge", edge: { ...edge, enabled: false, provenance: { source: "llm", run_id: "run", user_edited: true, rejected: true } } });
    expect(edited.graph?.edges[0].enabled).toBe(false);
    expect(graphEditorReducer(edited, { type: "undo" }).graph?.edges[0].enabled).toBe(true);
  });
});
