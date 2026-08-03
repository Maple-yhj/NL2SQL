import { useEffect, useMemo, useReducer, useState, type KeyboardEvent } from "react";
import type { ApiClient } from "../api";
import { graphEditorReducer } from "./relationshipGraphState";
import type { CatalogRelation, RelationshipCondition, RelationshipEdge, RelationshipGraphDraft, RelationshipGraphNode, RelationshipRoutePreview, RelationshipValidationReport, SemanticGraphBinding } from "../types";

const id = (prefix: string) => `${prefix}-${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`;

export function RelationshipGraphEditor({ api, sourceId, onActivated }: { api: ApiClient; sourceId: string; onActivated?: (binding: SemanticGraphBinding) => void }) {
  const [state, dispatch] = useReducer(graphEditorReducer, null, () => ({ graph: null as RelationshipGraphDraft | null, undo: [], redo: [] }));
  const [relations, setRelations] = useState<CatalogRelation[]>([]);
  const [message, setMessage] = useState("");
  const [validation, setValidation] = useState<RelationshipValidationReport | null>(null);
  const [route, setRoute] = useState<RelationshipRoutePreview | null>(null);
  const [routeNodes, setRouteNodes] = useState<string[]>([]);
  const [domainId, setDomainId] = useState("");
  const [fromNode, setFromNode] = useState("");
  const [toNode, setToNode] = useState("");
  const [fromColumn, setFromColumn] = useState("");
  const [toColumn, setToColumn] = useState("");
  const [pendingConditions, setPendingConditions] = useState<RelationshipCondition[]>([]);
  const [newRelation, setNewRelation] = useState("");
  const [newRole, setNewRole] = useState("");
  const [newEntity, setNewEntity] = useState("");

  useEffect(() => {
    let alive = true;
    void Promise.all([api.getRelationshipGraphDraft(sourceId), api.getDataSourceCatalog(sourceId)])
      .then(([graph, catalog]) => { if (alive) { dispatch({ type: "replace", graph }); setRelations(catalog.catalog.relations); setRouteNodes(graph.nodes.slice(0, 1).map((node) => node.node_id)); } })
      .catch(() => alive && setMessage("关系图草稿暂不可用"));
    return () => { alive = false; };
  }, [api, sourceId]);

  const graph = state.graph;
  const relationById = useMemo(() => new Map(relations.map((relation) => [relation.relation_id, relation])), [relations]);
  const columnsFor = (nodeId: string) => relationById.get(graph?.nodes.find((node) => node.node_id === nodeId)?.relation_id ?? "")?.columns ?? [];
  if (!graph) return <p className="datasource-empty">{message || "正在加载关系图草稿…"}</p>;

  const save = async () => {
    try { const saved = await api.saveRelationshipGraph(sourceId, { ...graph, revision: graph.revision + 1 }, graph.revision); dispatch({ type: "replace", graph: saved }); setMessage("关系图已保存"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "保存关系图失败"); }
  };
  const addEdge = () => {
    if (!fromNode || !toNode) return setMessage("请选择关联两端的角色");
    const current = fromColumn && toColumn ? [{ from_column_id: fromColumn, operator: "eq" as const, to_column_id: toColumn }] : [];
    const conditions = [...pendingConditions, ...current];
    if (!conditions.length) return setMessage("请至少添加一个字段条件");
    const unique = new Set(conditions.map((condition) => `${condition.from_column_id}:${condition.to_column_id}`));
    if (unique.size !== conditions.length) return setMessage("复合关联不能重复字段条件");
    const edge: RelationshipEdge = { edge_id: id("edge"), from_node_id: fromNode, to_node_id: toNode, conditions, cardinality: "unknown", join_semantics: "inner", preserve_node_id: null, route_priority: 100, enabled: true, provenance: { source: "user", user_edited: true }, quality: null };
    dispatch({ type: "addEdge", edge }); setPendingConditions([]); setFromColumn(""); setToColumn(""); setValidation(null); setRoute(null); setMessage("已新增未验证关系；保存并验证后再激活。");
  };
  const appendCondition = () => {
    if (!fromNode || !toNode || !fromColumn || !toColumn) return setMessage("请选择关联两端的字段");
    setPendingConditions((conditions) => [...conditions, { from_column_id: fromColumn, operator: "eq", to_column_id: toColumn }]);
    setFromColumn(""); setToColumn("");
  };
  const addNode = () => {
    if (!newRelation || !newRole.trim() || !newEntity.trim()) return setMessage("请选择物理表，并填写角色名和业务实体名");
    if (graph.nodes.some((node) => node.relation_id === newRelation && node.role_name.toLocaleLowerCase() === newRole.trim().toLocaleLowerCase())) return setMessage("同一物理表的角色名必须唯一");
    const node: RelationshipGraphNode = { node_id: id("node"), relation_id: newRelation, role_name: newRole.trim(), logical_entity: newEntity.trim(), enabled: true };
    dispatch({ type: "addNode", node }); setNewRole(""); setNewEntity(""); setValidation(null); setRoute(null);
  };
  const removeNode = (nodeId: string) => {
    const affected = graph.edges.filter((edge) => edge.from_node_id === nodeId || edge.to_node_id === nodeId).length;
    if (window.confirm(`删除该角色会同时删除 ${affected} 条关联边，是否继续？`)) dispatch({ type: "removeNode", nodeId });
  };
  const setRecommendationState = (edge: RelationshipEdge, rejected: boolean) => {
    dispatch({ type: "updateEdge", edge: { ...edge, enabled: !rejected, provenance: { ...edge.provenance, user_edited: true, rejected } } });
    setValidation(null); setRoute(null);
  };
  const validate = async () => { try { setValidation(await api.validateRelationshipGraph(sourceId, graph.graph_id)); setMessage("已完成本地关系验证"); } catch (error) { setMessage(error instanceof Error ? error.message : "验证失败"); } };
  const preview = async () => { try { setRoute(await api.previewRelationshipRoute(sourceId, graph.graph_id, routeNodes)); } catch (error) { setRoute(null); setMessage(error instanceof Error ? error.message : "无法解析该路径"); } };
  const savePreferredRoute = () => {
    if (!route || !route.steps.length) return;
    dispatch({ type: "replace", graph: { ...graph, route_rules: [...graph.route_rules, { rule_id: id("route"), terminal_node_ids: routeNodes, ordered_edge_ids: route.steps.map((step) => step.edge_id) }] } });
    setMessage("已将当前预览加入草稿首选路径；请保存草稿。");
  };
  const activate = async () => {
    if (!domainId.trim()) return setMessage("请先填写业务域 ID，再显式激活图版本");
    const mappings = graph.nodes.flatMap((node) => (relationById.get(node.relation_id)?.columns ?? []).map((column) => ({ logical_ref: `${domainId}.${node.logical_entity}.${column.name}`, node_id: node.node_id, column_id: column.column_id })));
    try { const binding = await api.activateRelationshipGraph(sourceId, graph.graph_id, { domain_id: domainId.trim(), mappings }); onActivated?.(binding); setMessage(`已激活不可变 v2 绑定 ${binding.binding_id}`); }
    catch (error) { setMessage(error instanceof Error ? error.message : "激活失败"); }
  };
  const onEditorKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (!event.metaKey && !event.ctrlKey) return;
    const key = event.key.toLocaleLowerCase();
    if (key === "s") { event.preventDefault(); void save(); return; }
    if (key === "z") {
      event.preventDefault();
      dispatch({ type: event.shiftKey ? "redo" : "undo" });
    }
  };

  return <section className="relationship-graph-editor" aria-label="关系图编辑器" tabIndex={-1} onKeyDown={onEditorKeyDown}>
    <div><h3>关系图草稿 · r{graph.revision}</h3><p>{graph.nodes.length} 个角色节点，{graph.edges.length} 条边；草稿变更必须保存、验证并显式激活。</p></div>
    <p className="relationship-graph-shortcuts">键盘：Cmd/Ctrl+S 保存；Cmd/Ctrl+Z 撤销；Cmd/Ctrl+Shift+Z 重做。</p>
    <div className="relationship-graph-actions"><button type="button" onClick={() => dispatch({ type: "undo" })} disabled={!state.undo.length}>撤销</button><button type="button" onClick={() => dispatch({ type: "redo" })} disabled={!state.redo.length}>重做</button><button type="button" onClick={() => void save()}>保存草稿</button><button type="button" onClick={() => void validate()}>验证图</button><button type="button" onClick={() => void api.rerunRelationshipRecommendations(sourceId).then(() => setMessage("已启动关联推荐"))}>重跑 AI 推荐</button></div>
    <h4>角色节点</h4><div className="relationship-graph-table">{graph.nodes.map((node) => <div key={node.node_id}><code>{node.role_name}</code><span>{relationById.get(node.relation_id)?.relation ?? node.relation_id} · {node.logical_entity}</span><button type="button" onClick={() => removeNode(node.node_id)}>删除角色</button></div>)}</div><div className="relationship-graph-actions"><select aria-label="物理表" value={newRelation} onChange={(event) => setNewRelation(event.target.value)}><option value="">新增角色的物理表</option>{relations.map((relation) => <option key={relation.relation_id} value={relation.relation_id}>{relation.relation}</option>)}</select><input aria-label="角色名" value={newRole} onChange={(event) => setNewRole(event.target.value)} placeholder="角色名，例如 manager" /><input aria-label="业务实体名" value={newEntity} onChange={(event) => setNewEntity(event.target.value)} placeholder="业务实体名，例如 Manager" /><button type="button" onClick={addNode}>新增角色</button></div>
    <h4>添加关联（支持复合字段条件）</h4><div className="relationship-graph-actions"><select aria-label="起始角色" value={fromNode} onChange={(event) => { setFromNode(event.target.value); setFromColumn(""); setPendingConditions([]); }}><option value="">起始角色</option>{graph.nodes.map((node) => <option key={node.node_id} value={node.node_id}>{node.role_name}</option>)}</select><select aria-label="起始字段" value={fromColumn} onChange={(event) => setFromColumn(event.target.value)}><option value="">起始字段</option>{columnsFor(fromNode).map((column) => <option key={column.column_id} value={column.column_id}>{column.name}</option>)}</select><select aria-label="目标角色" value={toNode} onChange={(event) => { setToNode(event.target.value); setToColumn(""); setPendingConditions([]); }}><option value="">目标角色</option>{graph.nodes.map((node) => <option key={node.node_id} value={node.node_id}>{node.role_name}</option>)}</select><select aria-label="目标字段" value={toColumn} onChange={(event) => setToColumn(event.target.value)}><option value="">目标字段</option>{columnsFor(toNode).map((column) => <option key={column.column_id} value={column.column_id}>{column.name}</option>)}</select><button type="button" onClick={appendCondition}>加入复合条件</button><button type="button" onClick={addEdge}>新增边</button></div>{pendingConditions.length > 0 && <p>待加入条件：{pendingConditions.length} 个；可继续选择字段添加。</p>}
    <h4>关联边</h4><div className="relationship-graph-table">{graph.edges.length ? graph.edges.map((edge) => <div key={edge.edge_id}><code>{edge.edge_id}</code><span>{edge.from_node_id} → {edge.to_node_id} · {edge.conditions.length} 个条件 · {String(edge.provenance.source ?? "unknown")}{edge.enabled ? "" : " · 已拒绝"}</span>{edge.provenance.source === "llm" && <><button type="button" onClick={() => setRecommendationState(edge, false)}>接受</button><button type="button" onClick={() => setRecommendationState(edge, true)}>拒绝</button></>}<button type="button" onClick={() => dispatch({ type: "removeEdge", edgeId: edge.edge_id })}>移除</button></div>) : <p>尚无关系边；可重跑 AI 推荐或手工新增。</p>}</div>
    <h4>路由预览</h4><div className="relationship-graph-actions">{graph.nodes.map((node) => <label key={node.node_id}><input type="checkbox" checked={routeNodes.includes(node.node_id)} onChange={(event) => setRouteNodes((nodes) => event.target.checked ? [...nodes, node.node_id] : nodes.filter((item) => item !== node.node_id))} />{node.role_name}</label>)}<button type="button" onClick={() => void preview()} disabled={!routeNodes.length}>预览路径</button></div>{route && <p>route {route.route_digest}：{route.steps.map((step) => step.edge_id).join(" → ") || "单节点（无 JOIN）"}{route.steps.length > 0 && <button type="button" onClick={savePreferredRoute}>设为首选路径</button>}</p>}
    <h4>激活</h4><div className="relationship-graph-actions"><input aria-label="业务域 ID" value={domainId} onChange={(event) => setDomainId(event.target.value)} placeholder="例如 dataset.sales" /><button type="button" onClick={() => void activate()} disabled={!validation?.activation_allowed}>显式激活 v2 图绑定</button></div>{validation && <div role="status"><p>{validation.activation_allowed ? "验证允许激活" : "存在阻断项，不能激活"} · {validation.report_digest}</p>{validation.findings.map((finding) => <p key={`${finding.code}-${finding.edge_id}`}>{finding.severity}: {finding.message}</p>)}</div>}
    {message && <p role="status">{message}</p>}
  </section>;
}
