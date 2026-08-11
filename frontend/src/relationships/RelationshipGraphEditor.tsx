import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  Database,
  GitBranch,
  GitMerge,
  Info,
  Loader2,
  Plus,
  Redo2,
  RefreshCw,
  Route,
  Save,
  ShieldCheck,
  Sparkles,
  Trash2,
  Undo2,
  Waypoints,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useState,
  type KeyboardEvent,
} from "react";
import { ApiError, type ApiClient } from "../api";
import type {
  CatalogRelation,
  RelationshipCondition,
  RelationshipEdge,
  RelationshipGraphDraft,
  RelationshipGraphNode,
  RelationshipRoutePreview,
  RelationshipValidationReport,
  SemanticGraphBinding,
} from "../types";
import { graphEditorReducer } from "./relationshipGraphState";

const id = (prefix: string) =>
  `${prefix}-${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`;

type LoadState = "loading" | "ready" | "missing" | "error";
type Notice = { tone: "info" | "success" | "warning" | "error"; text: string };

const provenanceLabels: Record<string, string> = {
  database_constraint: "数据库约束",
  llm: "AI 推荐",
  user: "手工配置",
};

const cardinalityLabels: Record<RelationshipEdge["cardinality"], string> = {
  one_to_one: "一对一",
  one_to_many: "一对多",
  many_to_one: "多对一",
  many_to_many: "多对多",
  unknown: "基数待确认",
};

type RelationshipFinding = RelationshipValidationReport["findings"][number];

function relationshipFindingText(
  finding: RelationshipFinding,
  nodeName: (nodeId: string) => string,
): string {
  const messages: Record<string, string> = {
    RELATIONSHIP_REVIEW_REQUIRED: "AI 推荐关系必须先接受或拒绝。",
    RELATIONSHIP_BLOCKED: "关系证据未通过结构或统计校验。",
    RELATIONSHIP_MANY_TO_MANY: "多对多关系必须先声明明确的数据粒度。",
    RELATIONSHIP_UNKNOWN_CARDINALITY: "请先确认关系基数。",
    RELATIONSHIP_HIGH_FANOUT: "关系展开倍数过高，无法安全激活。",
    RELATIONSHIP_LOW_MATCH_RATE: "关系键的观测匹配率较低。",
    RELATIONSHIP_CYCLE_UNRESOLVED: "关系图存在环路，请设置首选路径或移除冗余边。",
  };
  if (finding.code === "RELATIONSHIP_ISOLATED_ROLE" && finding.node_id) {
    return `角色 ${nodeName(finding.node_id)} 未连接，只能进行单表查询。`;
  }
  return messages[finding.code] ?? finding.message;
}

function relationshipErrorText(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  const messages: Record<string, string> = {
    "required graph nodes are not connected by safe edges":
      "所选角色之间没有已审核且安全的连接路径。",
    "graph binding already exists": "相同的关系绑定已经存在。",
    "relationship graph validation does not permit activation":
      "关系图仍有阻断项，暂时无法激活。",
  };
  return messages[error.message] ?? error.message;
}

export function RelationshipGraphEditor({
  api,
  sourceId,
  onActivated,
}: {
  api: ApiClient;
  sourceId: string;
  onActivated?: (binding: SemanticGraphBinding) => void;
}) {
  const [state, dispatch] = useReducer(graphEditorReducer, null, () => ({
    graph: null as RelationshipGraphDraft | null,
    undo: [],
    redo: [],
  }));
  const [relations, setRelations] = useState<CatalogRelation[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [activeAction, setActiveAction] = useState("");
  const [validation, setValidation] =
    useState<RelationshipValidationReport | null>(null);
  const [route, setRoute] = useState<RelationshipRoutePreview | null>(null);
  const [routeNodes, setRouteNodes] = useState<string[]>([]);
  const [domainId, setDomainId] = useState("");
  const [fromNode, setFromNode] = useState("");
  const [toNode, setToNode] = useState("");
  const [fromColumn, setFromColumn] = useState("");
  const [toColumn, setToColumn] = useState("");
  const [pendingConditions, setPendingConditions] = useState<
    RelationshipCondition[]
  >([]);
  const [newRelation, setNewRelation] = useState("");
  const [newRole, setNewRole] = useState("");
  const [newEntity, setNewEntity] = useState("");

  const loadGraph = useCallback(async () => {
    setLoadState("loading");
    setNotice(null);
    try {
      const [graph, catalog] = await Promise.all([
        api.getRelationshipGraphDraft(sourceId),
        api.getDataSourceCatalog(sourceId),
      ]);
      dispatch({ type: "replace", graph });
      setRelations(catalog.catalog.relations);
      setRouteNodes(graph.nodes.slice(0, 1).map((node) => node.node_id));
      setValidation(null);
      setRoute(null);
      setLoadState("ready");
      return true;
    } catch (error) {
      setLoadState(error instanceof ApiError && error.status === 404 ? "missing" : "error");
      setNotice({
        tone: "error",
        text:
          error instanceof ApiError && error.status === 404
            ? "当前数据源还没有关系图草稿，可先生成基础角色节点。"
            : error instanceof Error
              ? error.message
              : "关系图草稿加载失败，请稍后重试。",
      });
      return false;
    }
  }, [api, sourceId]);

  useEffect(() => {
    void loadGraph();
  }, [loadGraph]);

  const graph = state.graph;
  const relationById = useMemo(
    () => new Map(relations.map((relation) => [relation.relation_id, relation])),
    [relations],
  );
  const nodeById = useMemo(
    () => new Map((graph?.nodes ?? []).map((node) => [node.node_id, node])),
    [graph?.nodes],
  );
  const hasUnsavedChanges = state.undo.length > 0;

  const columnsFor = (nodeId: string) =>
    relationById.get(nodeById.get(nodeId)?.relation_id ?? "")?.columns ?? [];

  const columnName = (nodeId: string, columnId: string) =>
    columnsFor(nodeId).find((column) => column.column_id === columnId)?.name ??
    columnId;

  const nodeName = (nodeId: string) => nodeById.get(nodeId)?.role_name ?? nodeId;

  const resetDerivedState = () => {
    setValidation(null);
    setRoute(null);
  };

  const prepareDraft = async () => {
    setActiveAction("prepare");
    setNotice({ tone: "info", text: "正在生成基础角色节点与关联建议…" });
    try {
      await api.rerunRelationshipRecommendations(sourceId);
      const loaded = await loadGraph();
      if (loaded) {
        setNotice({
          tone: "success",
          text: "关系图草稿已生成。请核对角色和关联条件后保存、验证。",
        });
      }
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "关系图草稿生成失败。",
      });
    } finally {
      setActiveAction("");
    }
  };

  const save = async () => {
    if (!graph || !hasUnsavedChanges) return;
    setActiveAction("save");
    setNotice(null);
    try {
      const saved = await api.saveRelationshipGraph(
        sourceId,
        { ...graph, revision: graph.revision + 1 },
        graph.revision,
      );
      dispatch({ type: "replace", graph: saved });
      setNotice({ tone: "success", text: `草稿 r${saved.revision} 已保存。` });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "关系图保存失败。",
      });
    } finally {
      setActiveAction("");
    }
  };

  const addEdge = () => {
    if (!graph) return;
    if (!fromNode || !toNode) {
      setNotice({ tone: "warning", text: "请选择关联两端的角色。" });
      return;
    }
    if (fromNode === toNode) {
      setNotice({ tone: "warning", text: "起始角色和目标角色不能相同。" });
      return;
    }
    const current =
      fromColumn && toColumn
        ? [
            {
              from_column_id: fromColumn,
              operator: "eq" as const,
              to_column_id: toColumn,
            },
          ]
        : [];
    const conditions = [...pendingConditions, ...current];
    if (!conditions.length) {
      setNotice({ tone: "warning", text: "请至少添加一个字段条件。" });
      return;
    }
    const unique = new Set(
      conditions.map(
        (condition) =>
          `${condition.from_column_id}:${condition.to_column_id}`,
      ),
    );
    if (unique.size !== conditions.length) {
      setNotice({ tone: "warning", text: "复合关联不能重复字段条件。" });
      return;
    }
    const edge: RelationshipEdge = {
      edge_id: id("edge"),
      from_node_id: fromNode,
      to_node_id: toNode,
      conditions,
      cardinality: "unknown",
      join_semantics: "inner",
      preserve_node_id: null,
      route_priority: 100,
      enabled: true,
      provenance: { source: "user", user_edited: true },
      quality: null,
    };
    dispatch({ type: "addEdge", edge });
    setPendingConditions([]);
    setFromColumn("");
    setToColumn("");
    resetDerivedState();
    setNotice({
      tone: "info",
      text: "已加入未验证关联。保存草稿后再执行验证。",
    });
  };

  const appendCondition = () => {
    if (!fromNode || !toNode || !fromColumn || !toColumn) {
      setNotice({ tone: "warning", text: "请先选择关联两端的角色和字段。" });
      return;
    }
    const condition = {
      from_column_id: fromColumn,
      operator: "eq" as const,
      to_column_id: toColumn,
    };
    if (
      pendingConditions.some(
        (item) =>
          item.from_column_id === condition.from_column_id &&
          item.to_column_id === condition.to_column_id,
      )
    ) {
      setNotice({ tone: "warning", text: "这个字段条件已经在待加入列表中。" });
      return;
    }
    setPendingConditions((conditions) => [...conditions, condition]);
    setFromColumn("");
    setToColumn("");
    setNotice(null);
  };

  const addNode = () => {
    if (!graph) return;
    if (!newRelation || !newRole.trim() || !newEntity.trim()) {
      setNotice({
        tone: "warning",
        text: "请选择物理表，并填写角色名和业务实体名。",
      });
      return;
    }
    if (
      graph.nodes.some(
        (node) =>
          node.relation_id === newRelation &&
          node.role_name.toLocaleLowerCase() ===
            newRole.trim().toLocaleLowerCase(),
      )
    ) {
      setNotice({ tone: "warning", text: "同一物理表的角色名必须唯一。" });
      return;
    }
    const node: RelationshipGraphNode = {
      node_id: id("node"),
      relation_id: newRelation,
      role_name: newRole.trim(),
      logical_entity: newEntity.trim(),
      enabled: true,
    };
    dispatch({ type: "addNode", node });
    setNewRole("");
    setNewEntity("");
    resetDerivedState();
    setNotice({ tone: "info", text: "角色已加入草稿，保存后生效。" });
  };

  const removeNode = (nodeId: string) => {
    if (!graph) return;
    const affected = graph.edges.filter(
      (edge) => edge.from_node_id === nodeId || edge.to_node_id === nodeId,
    ).length;
    if (
      window.confirm(`删除该角色会同时删除 ${affected} 条关联边，是否继续？`)
    ) {
      dispatch({ type: "removeNode", nodeId });
      setRouteNodes((nodes) => nodes.filter((item) => item !== nodeId));
      resetDerivedState();
      setNotice({ tone: "info", text: "角色已从草稿移除，保存后生效。" });
    }
  };

  const setRecommendationState = (edge: RelationshipEdge, rejected: boolean) => {
    dispatch({
      type: "updateEdge",
      edge: {
        ...edge,
        enabled: !rejected,
        provenance: {
          ...edge.provenance,
          user_edited: true,
          rejected,
        },
      },
    });
    resetDerivedState();
    setNotice({
      tone: "info",
      text: rejected ? "已拒绝这条 AI 推荐。" : "已接受这条 AI 推荐。",
    });
  };

  const setEdgeCardinality = (
    edge: RelationshipEdge,
    cardinality: RelationshipEdge["cardinality"],
  ) => {
    dispatch({
      type: "updateEdge",
      edge: {
        ...edge,
        cardinality,
        enabled: true,
        provenance: {
          ...edge.provenance,
          user_edited: true,
          rejected: false,
        },
      },
    });
    resetDerivedState();
    setNotice({ tone: "info", text: "关联基数已更新，保存后重新验证。" });
  };

  const validate = async () => {
    if (!graph || hasUnsavedChanges) return;
    setActiveAction("validate");
    setNotice(null);
    try {
      const report = await api.validateRelationshipGraph(sourceId, graph.graph_id);
      setValidation(report);
      setNotice({
        tone: report.activation_allowed ? "success" : "warning",
        text: report.activation_allowed
          ? "关系图验证通过，可以激活。"
          : "验证完成，但仍有阻断项需要处理。",
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "关系图验证失败。",
      });
    } finally {
      setActiveAction("");
    }
  };

  const rerunRecommendations = async () => {
    setActiveAction("recommend");
    setNotice({ tone: "info", text: "正在重新生成关联建议…" });
    try {
      await api.rerunRelationshipRecommendations(sourceId);
      setNotice({
        tone: "success",
        text: "关联推荐任务已启动，稍后重新加载草稿即可查看结果。",
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "关联推荐启动失败。",
      });
    } finally {
      setActiveAction("");
    }
  };

  const preview = async () => {
    if (!graph || !routeNodes.length) return;
    setActiveAction("preview");
    setNotice(null);
    try {
      setRoute(
        await api.previewRelationshipRoute(
          sourceId,
          graph.graph_id,
          routeNodes,
        ),
      );
    } catch (error) {
      setRoute(null);
      setNotice({
        tone: "error",
        text: relationshipErrorText(error, "无法解析这条查询路径。"),
      });
    } finally {
      setActiveAction("");
    }
  };

  const savePreferredRoute = () => {
    if (!graph || !route || !route.steps.length) return;
    dispatch({
      type: "updateRouteRules",
      routeRules: [
        ...graph.route_rules,
        {
          rule_id: id("route"),
          terminal_node_ids: routeNodes,
          ordered_edge_ids: route.steps.map((step) => step.edge_id),
        },
      ],
    });
    setNotice({
      tone: "info",
      text: "当前预览已加入首选路径，请保存草稿。",
    });
  };

  const activate = async () => {
    if (!graph) return;
    if (!domainId.trim()) {
      setNotice({ tone: "warning", text: "请先填写业务域 ID。" });
      return;
    }
    const mappings = graph.nodes.flatMap((node) =>
      (relationById.get(node.relation_id)?.columns ?? []).map((column) => ({
        logical_ref: `${domainId}.${node.logical_entity}.${column.name}`,
        node_id: node.node_id,
        column_id: column.column_id,
      })),
    );
    setActiveAction("activate");
    setNotice(null);
    try {
      const binding = await api.activateRelationshipGraph(
        sourceId,
        graph.graph_id,
        { domain_id: domainId.trim(), mappings },
      );
      onActivated?.(binding);
      setNotice({
        tone: "success",
        text: `不可变 v2 绑定 ${binding.binding_id} 已激活。`,
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: relationshipErrorText(error, "关系图激活失败。"),
      });
    } finally {
      setActiveAction("");
    }
  };

  const undo = () => {
    dispatch({ type: "undo" });
    resetDerivedState();
  };

  const redo = () => {
    dispatch({ type: "redo" });
    resetDerivedState();
  };

  const onEditorKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (!event.metaKey && !event.ctrlKey) return;
    const key = event.key.toLocaleLowerCase();
    if (key === "s") {
      event.preventDefault();
      void save();
      return;
    }
    if (key === "z") {
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    }
  };

  if (loadState !== "ready" || !graph) {
    const isLoading = loadState === "loading";
    const isMissing = loadState === "missing";
    return (
      <section className="relationship-graph-editor is-empty" aria-label="关系图编辑器">
        <div className="relationship-empty-state" role={isLoading ? "status" : "alert"}>
          <span className="relationship-empty-icon">
            {isLoading ? (
              <Loader2 className="spin" size={22} />
            ) : (
              <GitMerge size={22} />
            )}
          </span>
          <div>
            <h3>{isLoading ? "正在读取关系图草稿" : "关系图草稿尚未就绪"}</h3>
            <p>
              {isLoading
                ? "正在同步角色节点、关联边和验证状态。"
                : notice?.text ?? "请检查数据源状态后重试。"}
            </p>
          </div>
          {!isLoading && (
            <button
              className={isMissing ? "relationship-button primary" : "relationship-button"}
              type="button"
              onClick={() => void (isMissing ? prepareDraft() : loadGraph())}
              disabled={Boolean(activeAction)}
            >
              {activeAction ? (
                <Loader2 className="spin" size={16} />
              ) : isMissing ? (
                <Sparkles size={16} />
              ) : (
                <RefreshCw size={16} />
              )}
              {isMissing ? "生成关系图草稿" : "重新加载"}
            </button>
          )}
        </div>
      </section>
    );
  }

  const graphReady = validation?.activation_allowed ?? false;
  const graphStatus = hasUnsavedChanges
    ? "有未保存修改"
    : graphReady
      ? "验证已通过"
      : "草稿已保存";

  return (
    <section
      className="relationship-graph-editor"
      aria-label="关系图编辑器"
      tabIndex={-1}
      onKeyDown={onEditorKeyDown}
    >
      <header className="relationship-graph-header">
        <div className="relationship-graph-title">
          <span className="relationship-title-icon">
            <Waypoints size={20} />
          </span>
          <div>
            <h3>关系图草稿</h3>
            <p>用角色节点和关联边定义跨表查询路径。</p>
          </div>
        </div>
        <div className="relationship-graph-summary" aria-label="关系图摘要">
          <span className="relationship-revision">r{graph.revision}</span>
          <span>{graph.nodes.length} 个角色</span>
          <span>{graph.edges.length} 条关联</span>
          <strong
            className={
              hasUnsavedChanges ? "warning" : graphReady ? "success" : ""
            }
          >
            {graphReady && <CheckCircle2 size={13} />}
            {hasUnsavedChanges && <AlertTriangle size={13} />}
            {graphStatus}
          </strong>
        </div>
      </header>

      <div className="relationship-graph-toolbar">
        <div className="relationship-history-actions" aria-label="编辑历史">
          <button
            className="relationship-icon-button"
            type="button"
            onClick={undo}
            disabled={!state.undo.length || Boolean(activeAction)}
            aria-label="撤销关系图修改"
            title="撤销（Cmd/Ctrl+Z）"
          >
            <Undo2 size={16} />
          </button>
          <button
            className="relationship-icon-button"
            type="button"
            onClick={redo}
            disabled={!state.redo.length || Boolean(activeAction)}
            aria-label="重做关系图修改"
            title="重做（Cmd/Ctrl+Shift+Z）"
          >
            <Redo2 size={16} />
          </button>
          <span className="relationship-graph-shortcuts">Cmd/Ctrl+S 保存</span>
        </div>
        <div className="relationship-primary-actions">
          <button
            className="relationship-button"
            type="button"
            onClick={() => void rerunRecommendations()}
            disabled={Boolean(activeAction)}
          >
            {activeAction === "recommend" ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <Sparkles size={16} />
            )}
            重新推荐
          </button>
          <button
            className="relationship-button"
            type="button"
            onClick={() => void validate()}
            disabled={
              hasUnsavedChanges || Boolean(activeAction) || !graph.nodes.length
            }
            title={hasUnsavedChanges ? "请先保存草稿" : undefined}
          >
            {activeAction === "validate" ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <ShieldCheck size={16} />
            )}
            验证关系图
          </button>
          <button
            className="relationship-button primary"
            type="button"
            onClick={() => void save()}
            disabled={!hasUnsavedChanges || Boolean(activeAction)}
          >
            {activeAction === "save" ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <Save size={16} />
            )}
            保存草稿
          </button>
        </div>
      </div>

      <section className="relationship-overview" aria-labelledby="relationship-overview-title">
        <div className="relationship-section-heading">
          <div>
            <h4 id="relationship-overview-title">关系结构概览</h4>
            <p>角色代表表在业务语境中的用途，关联边决定查询时如何连接。</p>
          </div>
          <span className={graph.edges.length ? "ready" : ""}>
            {graph.edges.length ? "已形成查询路径" : "等待添加关联"}
          </span>
        </div>

        {graph.nodes.length ? (
          <div className="relationship-canvas">
            <div className="relationship-node-grid" aria-label="角色节点概览">
              {graph.nodes.map((node) => {
                const relation = relationById.get(node.relation_id);
                return (
                  <article className="relationship-node-card" key={node.node_id}>
                    <span className="relationship-node-icon">
                      <Database size={16} />
                    </span>
                    <div>
                      <strong>{node.role_name}</strong>
                      <code title={relation?.relation ?? node.relation_id}>
                        {relation?.relation ?? node.relation_id}
                      </code>
                      <small>
                        {node.logical_entity} · {relation?.columns.length ?? 0} 个字段
                      </small>
                    </div>
                    <span className={node.enabled ? "node-state enabled" : "node-state"}>
                      {node.enabled ? "启用" : "停用"}
                    </span>
                  </article>
                );
              })}
            </div>

            {graph.edges.length ? (
              <div className="relationship-edge-map" aria-label="关联边概览">
                {graph.edges.map((edge) => (
                  <article
                    className={`relationship-edge-card${edge.enabled ? "" : " disabled"}`}
                    key={edge.edge_id}
                  >
                    <div className="relationship-edge-endpoint">
                      <span>起始角色</span>
                      <strong>{nodeName(edge.from_node_id)}</strong>
                    </div>
                    <div className="relationship-edge-connector">
                      <span aria-hidden="true" />
                      <div>
                        <strong>
                          {edge.join_semantics === "left" ? "LEFT JOIN" : "INNER JOIN"}
                        </strong>
                        <small>
                          {edge.conditions.length} 个条件 · {cardinalityLabels[edge.cardinality]}
                        </small>
                      </div>
                      <ArrowRight size={16} aria-hidden="true" />
                    </div>
                    <div className="relationship-edge-endpoint target">
                      <span>目标角色</span>
                      <strong>{nodeName(edge.to_node_id)}</strong>
                    </div>
                    <div className="relationship-edge-condition">
                      {edge.conditions.map((condition) => (
                        <code
                          key={`${condition.from_column_id}-${condition.to_column_id}`}
                        >
                          {columnName(edge.from_node_id, condition.from_column_id)} ={" "}
                          {columnName(edge.to_node_id, condition.to_column_id)}
                        </code>
                      ))}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <div className="relationship-inline-empty">
                <GitBranch size={18} />
                <span>角色节点已就绪，在下方添加第一条关联。</span>
              </div>
            )}
          </div>
        ) : (
          <div className="relationship-inline-empty">
            <Database size={18} />
            <span>草稿中还没有角色节点，请先从物理表添加角色。</span>
          </div>
        )}
      </section>

      <section className="relationship-editor-section" aria-labelledby="relationship-nodes-title">
        <div className="relationship-section-heading compact">
          <div>
            <h4 id="relationship-nodes-title">角色节点</h4>
            <p>同一张物理表可以用不同角色参与关系图。</p>
          </div>
          <span>{graph.nodes.length} 个</span>
        </div>

        {graph.nodes.length > 0 && (
          <div className="relationship-node-list">
            {graph.nodes.map((node) => {
              const relation = relationById.get(node.relation_id);
              return (
                <div className="relationship-node-row" key={node.node_id}>
                  <span className="relationship-row-icon">
                    <Database size={15} />
                  </span>
                  <div>
                    <strong>{node.role_name}</strong>
                    <span>{relation?.relation ?? node.relation_id}</span>
                  </div>
                  <code>{node.logical_entity}</code>
                  <button
                    className="relationship-text-button danger"
                    type="button"
                    onClick={() => removeNode(node.node_id)}
                  >
                    <Trash2 size={14} />
                    移除
                  </button>
                </div>
              );
            })}
          </div>
        )}

        <div className="relationship-form-grid node-form">
          <label>
            <span>物理表</span>
            <select
              value={newRelation}
              onChange={(event) => setNewRelation(event.target.value)}
            >
              <option value="">选择物理表</option>
              {relations.map((relation) => (
                <option key={relation.relation_id} value={relation.relation_id}>
                  {relation.relation}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>角色名</span>
            <input
              value={newRole}
              onChange={(event) => setNewRole(event.target.value)}
              placeholder="例如 manager"
            />
          </label>
          <label>
            <span>业务实体名</span>
            <input
              value={newEntity}
              onChange={(event) => setNewEntity(event.target.value)}
              placeholder="例如 Manager"
            />
          </label>
          <button
            className="relationship-button"
            type="button"
            onClick={addNode}
            disabled={Boolean(activeAction)}
          >
            <Plus size={16} />
            添加角色
          </button>
        </div>
      </section>

      <section className="relationship-editor-section" aria-labelledby="relationship-edges-title">
        <div className="relationship-section-heading compact">
          <div>
            <h4 id="relationship-edges-title">关联条件</h4>
            <p>选择两个角色及字段；复合关联可逐条暂存字段条件。</p>
          </div>
          <span>{graph.edges.length} 条</span>
        </div>

        <div className="relationship-join-builder">
          <div className="relationship-form-grid edge-form">
            <label>
              <span>起始角色</span>
              <select
                value={fromNode}
                onChange={(event) => {
                  setFromNode(event.target.value);
                  setFromColumn("");
                  setPendingConditions([]);
                }}
              >
                <option value="">选择角色</option>
                {graph.nodes.map((node) => (
                  <option key={node.node_id} value={node.node_id}>
                    {node.role_name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>起始字段</span>
              <select
                value={fromColumn}
                onChange={(event) => setFromColumn(event.target.value)}
                disabled={!fromNode}
              >
                <option value="">选择字段</option>
                {columnsFor(fromNode).map((column) => (
                  <option key={column.column_id} value={column.column_id}>
                    {column.name}
                  </option>
                ))}
              </select>
            </label>
            <span className="relationship-join-arrow" aria-hidden="true">
              <GitMerge size={17} />
            </span>
            <label>
              <span>目标角色</span>
              <select
                value={toNode}
                onChange={(event) => {
                  setToNode(event.target.value);
                  setToColumn("");
                  setPendingConditions([]);
                }}
              >
                <option value="">选择角色</option>
                {graph.nodes.map((node) => (
                  <option key={node.node_id} value={node.node_id}>
                    {node.role_name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>目标字段</span>
              <select
                value={toColumn}
                onChange={(event) => setToColumn(event.target.value)}
                disabled={!toNode}
              >
                <option value="">选择字段</option>
                {columnsFor(toNode).map((column) => (
                  <option key={column.column_id} value={column.column_id}>
                    {column.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {pendingConditions.length > 0 && (
            <div className="relationship-condition-queue" aria-label="待加入的复合条件">
              <span>待加入条件</span>
              <div>
                {pendingConditions.map((condition, index) => (
                  <span
                    className="relationship-condition-chip"
                    key={`${condition.from_column_id}-${condition.to_column_id}`}
                  >
                    <code>
                      {columnName(fromNode, condition.from_column_id)} ={" "}
                      {columnName(toNode, condition.to_column_id)}
                    </code>
                    <button
                      type="button"
                      onClick={() =>
                        setPendingConditions((conditions) =>
                          conditions.filter((_, itemIndex) => itemIndex !== index),
                        )
                      }
                      aria-label={`移除条件 ${index + 1}`}
                    >
                      <X size={12} />
                    </button>
                  </span>
                ))}
              </div>
            </div>
          )}

          <div className="relationship-builder-actions">
            <button
              className="relationship-text-button"
              type="button"
              onClick={appendCondition}
              disabled={!fromColumn || !toColumn}
            >
              <Plus size={14} />
              暂存为复合条件
            </button>
            <button
              className="relationship-button primary"
              type="button"
              onClick={addEdge}
              disabled={!fromNode || !toNode || Boolean(activeAction)}
            >
              <GitBranch size={16} />
              添加关联
            </button>
          </div>
        </div>

        {graph.edges.length ? (
          <div className="relationship-edge-list">
            {graph.edges.map((edge) => {
              const source = String(edge.provenance.source ?? "unknown");
              const reviewed = Boolean(edge.provenance.user_edited);
              const rejected = Boolean(edge.provenance.rejected);
              const reviewState =
                source !== "llm"
                  ? "neutral"
                  : rejected
                    ? "rejected"
                    : reviewed
                      ? "accepted"
                      : "pending";
              return (
                <div
                  className={`relationship-edge-row${edge.enabled ? "" : " disabled"}`}
                  key={edge.edge_id}
                >
                  <span className="relationship-row-icon">
                    <GitBranch size={15} />
                  </span>
                  <div>
                    <strong>
                      {nodeName(edge.from_node_id)}
                      <ArrowRight size={13} />
                      {nodeName(edge.to_node_id)}
                    </strong>
                    <span>
                      {edge.conditions.length} 个字段条件 · {cardinalityLabels[edge.cardinality]}
                    </span>
                  </div>
                  <span className={`relationship-edge-source ${reviewState}`}>
                    {provenanceLabels[source] ?? "来源未知"}
                    {source === "llm" &&
                      ` · ${rejected ? "已拒绝" : reviewed ? "已接受" : "待审核"}`}
                  </span>
                  <div className="relationship-row-actions">
                    <label className="relationship-cardinality-control">
                      <span>关系基数</span>
                      <select
                        aria-label={`${nodeName(edge.from_node_id)} 到 ${nodeName(edge.to_node_id)} 的关系基数`}
                        value={edge.cardinality}
                        onChange={(event) =>
                          setEdgeCardinality(
                            edge,
                            event.target.value as RelationshipEdge["cardinality"],
                          )
                        }
                      >
                        {Object.entries(cardinalityLabels).map(([value, label]) => (
                          <option key={value} value={value}>{label}</option>
                        ))}
                      </select>
                      <ChevronDown size={13} aria-hidden="true" />
                    </label>
                    {source === "llm" && (
                      <>
                        <button
                          className="relationship-text-button relationship-review-button accept"
                          type="button"
                          onClick={() => setRecommendationState(edge, false)}
                          disabled={reviewed && !rejected}
                        >
                          <Check size={14} />
                          接受
                        </button>
                        <button
                          className="relationship-text-button relationship-review-button reject"
                          type="button"
                          onClick={() => setRecommendationState(edge, true)}
                          disabled={rejected}
                        >
                          <X size={14} />
                          拒绝
                        </button>
                      </>
                    )}
                    <button
                      className="relationship-text-button danger"
                      type="button"
                      onClick={() => {
                        dispatch({ type: "removeEdge", edgeId: edge.edge_id });
                        resetDerivedState();
                        setNotice({
                          tone: "info",
                          text: "关联已从草稿移除，保存后生效。",
                        });
                      }}
                    >
                      <Trash2 size={14} />
                      移除
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="relationship-inline-empty compact">
            <GitBranch size={17} />
            <span>尚无关联边，可重新推荐或手工添加。</span>
          </div>
        )}
      </section>

      <div className="relationship-final-grid">
        <section className="relationship-final-section" aria-labelledby="relationship-route-title">
          <div className="relationship-section-heading compact">
            <div>
              <h4 id="relationship-route-title">查询路径预览</h4>
              <p>选择分析需要触达的角色，确认系统采用的连接路径。</p>
            </div>
            <Route size={18} />
          </div>
          <div className="relationship-route-options">
            {graph.nodes.map((node) => (
              <label key={node.node_id}>
                <input
                  type="checkbox"
                  checked={routeNodes.includes(node.node_id)}
                  onChange={(event) => {
                    setRouteNodes((nodes) =>
                      event.target.checked
                        ? [...nodes, node.node_id]
                        : nodes.filter((item) => item !== node.node_id),
                    );
                    setRoute(null);
                  }}
                />
                <span>{node.role_name}</span>
              </label>
            ))}
          </div>
          <button
            className="relationship-button"
            type="button"
            onClick={() => void preview()}
            disabled={!routeNodes.length || Boolean(activeAction) || hasUnsavedChanges}
            title={hasUnsavedChanges ? "请先保存草稿" : undefined}
          >
            {activeAction === "preview" ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <Route size={16} />
            )}
            预览查询路径
          </button>
          {route && (
            <div className="relationship-route-result">
              <div>
                <CheckCircle2 size={16} />
                <span>
                  {route.steps.length
                    ? route.steps
                        .map((step) => {
                          const edge = graph.edges.find(
                            (item) => item.edge_id === step.edge_id,
                          );
                          return edge
                            ? `${nodeName(edge.from_node_id)} → ${nodeName(edge.to_node_id)}`
                            : step.edge_id;
                        })
                        .join(" · ")
                    : "单角色路径，无需 JOIN"}
                </span>
              </div>
              {route.steps.length > 0 && (
                <button
                  className="relationship-text-button"
                  type="button"
                  onClick={savePreferredRoute}
                >
                  设为首选路径
                </button>
              )}
            </div>
          )}
        </section>

        <section className="relationship-final-section" aria-labelledby="relationship-activate-title">
          <div className="relationship-section-heading compact">
            <div>
              <h4 id="relationship-activate-title">验证与激活</h4>
              <p>验证通过后生成不可变 v2 绑定，供后续分析使用。</p>
            </div>
            <ShieldCheck size={18} />
          </div>
          <label className="relationship-domain-field">
            <span>业务域 ID</span>
            <input
              value={domainId}
              onChange={(event) => setDomainId(event.target.value)}
              placeholder="例如 dataset.sales"
            />
          </label>
          <button
            className="relationship-button primary"
            type="button"
            onClick={() => void activate()}
            disabled={
              !validation?.activation_allowed ||
              hasUnsavedChanges ||
              Boolean(activeAction)
            }
          >
            {activeAction === "activate" ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <CheckCircle2 size={16} />
            )}
            激活 v2 关系绑定
          </button>
          {!validation && (
            <p className="relationship-activation-hint">
              <Info size={14} />
              保存草稿并通过验证后可激活。
            </p>
          )}
        </section>
      </div>

      {validation && (
        <div
          className={`relationship-validation${validation.activation_allowed ? " success" : " warning"}`}
          role="status"
        >
          {validation.activation_allowed ? (
            <CheckCircle2 size={18} />
          ) : (
            <AlertTriangle size={18} />
          )}
          <div>
            <strong>
              {validation.activation_allowed ? "验证通过" : "存在阻断项"}
            </strong>
            <span>报告 {validation.report_digest}</span>
            {validation.findings.map((finding, index) => (
              <p key={`${finding.code}-${finding.edge_id ?? finding.node_id ?? index}`}>
                {finding.severity === "error" ? "错误" : "警告"}：
                {relationshipFindingText(finding, nodeName)}
              </p>
            ))}
          </div>
        </div>
      )}

      {notice && (
        <div className={`relationship-notice ${notice.tone}`} role="status" aria-live="polite">
          {notice.tone === "success" ? (
            <CheckCircle2 size={16} />
          ) : notice.tone === "warning" || notice.tone === "error" ? (
            <AlertTriangle size={16} />
          ) : (
            <Info size={16} />
          )}
          <span>{notice.text}</span>
        </div>
      )}
    </section>
  );
}
