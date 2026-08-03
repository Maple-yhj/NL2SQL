import type { RelationshipEdge, RelationshipGraphDraft, RelationshipGraphNode } from "../types";

export interface GraphEditorState { graph: RelationshipGraphDraft | null; undo: RelationshipGraphDraft[]; redo: RelationshipGraphDraft[]; }
export type GraphAction =
  | { type: "replace"; graph: RelationshipGraphDraft }
  | { type: "addEdge"; edge: RelationshipEdge }
  | { type: "updateEdge"; edge: RelationshipEdge }
  | { type: "removeEdge"; edgeId: string }
  | { type: "addNode"; node: RelationshipGraphNode }
  | { type: "removeNode"; nodeId: string }
  | { type: "undo" }
  | { type: "redo" };

export function graphEditorReducer(state: GraphEditorState, action: GraphAction): GraphEditorState {
  if (action.type === "undo") {
    const previous = state.undo.at(-1);
    return previous && state.graph ? { graph: previous, undo: state.undo.slice(0, -1), redo: [state.graph, ...state.redo] } : state;
  }
  if (action.type === "redo") {
    const next = state.redo[0];
    return next && state.graph ? { graph: next, undo: [...state.undo, state.graph], redo: state.redo.slice(1) } : state;
  }
  if (action.type === "replace") return { graph: action.graph, undo: [], redo: [] };
  if (!state.graph) return state;
  const graph = action.type === "addEdge"
    ? { ...state.graph, edges: [...state.graph.edges, action.edge] }
    : action.type === "updateEdge"
      ? { ...state.graph, edges: state.graph.edges.map((edge) => edge.edge_id === action.edge.edge_id ? action.edge : edge) }
    : action.type === "removeEdge"
      ? { ...state.graph, edges: state.graph.edges.filter((edge) => edge.edge_id !== action.edgeId) }
      : action.type === "addNode"
        ? { ...state.graph, nodes: [...state.graph.nodes, action.node] }
        : { ...state.graph, nodes: state.graph.nodes.filter((node) => node.node_id !== action.nodeId), edges: state.graph.edges.filter((edge) => edge.from_node_id !== action.nodeId && edge.to_node_id !== action.nodeId) };
  return { graph, undo: [...state.undo, state.graph], redo: [] };
}
