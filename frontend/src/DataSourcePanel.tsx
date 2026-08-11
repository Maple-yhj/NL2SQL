import {
  ArrowLeft,
  ArrowRight,
  AlertTriangle,
  Check,
  CheckCircle2,
  Database,
  FileCheck2,
  FileSpreadsheet,
  GitMerge,
  Layers3,
  Loader2,
  Plus,
  ShieldCheck,
  Table2,
  Trash2,
  Upload,
  Waypoints,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import type { ApiClient } from "./api";
import type {
  DataSource,
  DataSourceCatalog,
  CatalogRelation,
  AnySemanticBinding,
  SemanticBinding,
  SemanticFieldMetadata,
  SemanticFieldMapping,
  SemanticGraphBinding,
  SemanticMetricDefinition,
  SemanticRelationship,
} from "./types";
import { RelationshipGraphEditor } from "./relationships/RelationshipGraphEditor";
import {
  compactSemanticFieldMetadata,
  compactSemanticMetrics,
  SemanticDefinitionEditor,
  semanticMetricError,
} from "./SemanticDefinitionEditor";

interface DataSourcePanelProps {
  api: ApiClient;
  sources: DataSource[];
  selectedBinding: AnySemanticBinding | null;
  onRefresh: () => Promise<DataSource[]>;
  onBindingSelect: (binding: AnySemanticBinding | null) => void;
  onClose: () => void;
}

export function isGraphBinding(
  binding: AnySemanticBinding | null | undefined,
): binding is SemanticGraphBinding {
  return Boolean(
    binding &&
      "schema_version" in binding &&
      binding.schema_version === 2,
  );
}

export function selectPreferredActiveBinding(
  bindings: AnySemanticBinding[],
  selectedBinding: AnySemanticBinding | null,
): AnySemanticBinding | null {
  const active = bindings.filter((binding) => binding.status === "active");
  const selected = active.find(
    (binding) => binding.binding_id === selectedBinding?.binding_id,
  );
  if (selected) return selected;
  return (
    [...active].sort((left, right) => {
      const graphPriority = Number(isGraphBinding(right)) - Number(isGraphBinding(left));
      return (
        graphPriority ||
        right.version - left.version ||
        right.updated_at.localeCompare(left.updated_at)
      );
    })[0] ?? null
  );
}

function bindingStatusLabel(status: AnySemanticBinding["status"]): string {
  return { active: "已激活", draft: "草稿", retired: "已停用" }[status];
}

export function DataSourcePanel({
  api,
  sources,
  selectedBinding,
  onRefresh,
  onBindingSelect,
  onClose,
}: DataSourcePanelProps) {
  const [selectedId, setSelectedId] = useState(
    selectedBinding?.source_id ?? sources[0]?.source_id ?? "",
  );
  const [catalog, setCatalog] = useState<DataSourceCatalog | null>(null);
  const [bindings, setBindings] = useState<AnySemanticBinding[]>([]);
  const [relationName, setRelationName] = useState("");
  const [selectedRelations, setSelectedRelations] = useState<string[]>([]);
  const [primaryRelation, setPrimaryRelation] = useState("");
  const [relationships, setRelationships] = useState<SemanticRelationship[]>([]);
  const [domainId, setDomainId] = useState("");
  const [logicalRefs, setLogicalRefs] = useState<Record<string, string>>({});
  const [fieldMetadata, setFieldMetadata] = useState<
    Record<string, SemanticFieldMetadata>
  >({});
  const [metrics, setMetrics] = useState<SemanticMetricDefinition[]>([]);
  const [name, setName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [justActivated, setJustActivated] = useState(false);
  const [deleteCandidate, setDeleteCandidate] = useState<DataSource | null>(null);

  useEffect(() => {
    if (!selectedId) {
      setSelectedId(selectedBinding?.source_id ?? sources[0]?.source_id ?? "");
    }
  }, [selectedBinding, selectedId, sources]);

  useEffect(() => {
    const source = sources.find((item) => item.source_id === selectedId);
    if (!source || source.active_snapshot_version < 1) {
      setCatalog(null);
      return;
    }
    let active = true;
    setError("");
    void Promise.all([
      api.getDataSourceCatalog(source.source_id),
      api.listDataSourceBindings(source.source_id),
    ])
      .then(([payload, bindingPayload]) => {
        if (active) {
          const activeSemanticBinding = selectPreferredActiveBinding(
            bindingPayload.items,
            selectedBinding,
          );
          setCatalog(payload);
          setBindings(bindingPayload.items);
          onBindingSelect(activeSemanticBinding);
          const firstRelation = payload.catalog.relations[0];
          setRelationName((current) =>
            payload.catalog.relations.some(
              (relation) => relation.relation === current,
            )
              ? current
              : firstRelation?.relation ?? "",
          );
          if (activeSemanticBinding && !("schema_version" in activeSemanticBinding && activeSemanticBinding.schema_version === 2)) {
            const mappedRelations = Array.from(
              new Set(
                activeSemanticBinding.mappings.map(
                  (mapping) => mapping.physical_relation,
                ),
              ),
            );
            const primary =
              activeSemanticBinding.primary_relation ??
              mappedRelations[0] ??
              firstRelation?.relation ??
              "";
            setPrimaryRelation(primary);
            setSelectedRelations(
              Array.from(
                new Set([
                  primary,
                  ...(activeSemanticBinding.relationships ?? []).map(
                    (relationship) => relationship.right_relation,
                  ),
                  ...mappedRelations,
                ]),
              ).filter(Boolean),
            );
            setRelationships(activeSemanticBinding.relationships ?? []);
            setFieldMetadata(
              Object.fromEntries(
                activeSemanticBinding.mappings.map((mapping) => [
                  mappingKey(mapping.physical_relation, mapping.physical_column),
                  compactSemanticFieldMetadata(mapping),
                ]),
              ),
            );
            setMetrics(activeSemanticBinding.metrics ?? []);
          } else {
            const initialRelation = firstRelation?.relation ?? "";
            setPrimaryRelation(initialRelation);
            setSelectedRelations(initialRelation ? [initialRelation] : []);
            setRelationships([]);
            setFieldMetadata({});
            setMetrics([]);
          }
          setDomainId((current) => current || `dataset.${source.source_id}`);
          setLogicalRefs((current) => {
            const next = { ...current };
            for (const relation of payload.catalog.relations) {
              const entity = logicalEntityName(relation.relation);
              for (const column of relation.columns) {
                const key = mappingKey(relation.relation, column.name);
                next[key] ||= `dataset.${entity}.${column.name}`;
              }
            }
            if (
              activeSemanticBinding &&
              !("schema_version" in activeSemanticBinding && activeSemanticBinding.schema_version === 2)
            ) {
              for (const mapping of activeSemanticBinding.mappings) {
                next[mappingKey(mapping.physical_relation, mapping.physical_column)] =
                  mapping.logical_ref;
              }
            }
            return next;
          });
        }
      })
      .catch((reason) => {
        if (active) {
          setCatalog(null);
          setBindings([]);
          onBindingSelect(null);
          setError(reason instanceof Error ? reason.message : "无法读取数据目录");
        }
      });
    return () => {
      active = false;
    };
  }, [api, onBindingSelect, selectedId, sources]);

  useEffect(() => {
    setDomainId(selectedId ? `dataset.${selectedId}` : "");
    setLogicalRefs({});
    setFieldMetadata({});
    setMetrics([]);
    setSelectedRelations([]);
    setPrimaryRelation("");
    setRelationships([]);
    setJustActivated(false);
  }, [selectedId]);

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim() || !files.length || busy) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const sqliteFiles = files.filter((file) =>
        /\.(db|sqlite|sqlite3)$/i.test(file.name),
      );
      const created =
        sqliteFiles.length === 1 && files.length === 1
          ? await api.uploadSqliteDataSource(name.trim(), sqliteFiles[0])
          : await api.uploadFileDataSource(name.trim(), files);
      await onRefresh();
      setSelectedId(created.source_id);
      setName("");
      setFiles([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "数据源上传失败");
    } finally {
      setBusy(false);
    }
  }

  function addRelationToDataset(relation: string) {
    if (!catalog || selectedRelations.includes(relation)) {
      return;
    }
    if (!selectedRelations.length) {
      setSelectedRelations([relation]);
      setPrimaryRelation(relation);
      return;
    }
    const leftRelation = primaryRelation || selectedRelations[0];
    const relationship = suggestedRelationship(
      catalog.catalog.relations,
      leftRelation,
      relation,
      selectedRelations.length,
    );
    setSelectedRelations((current) => [...current, relation]);
    setRelationships((current) => [...current, relationship]);
  }

  function removeRelationFromDataset(relation: string) {
    if (!catalog || relation === primaryRelation) {
      return;
    }
    const nextRelations = selectedRelations.filter((item) => item !== relation);
    setSelectedRelations(nextRelations);
    setRelationships((current) =>
      current
        .filter((item) => item.right_relation !== relation)
        .map((item, index) => {
          const rightIndex = nextRelations.indexOf(item.right_relation);
          const leftIndex = nextRelations.indexOf(item.left_relation);
          if (leftIndex >= 0 && leftIndex < rightIndex) {
            return item;
          }
          return suggestedRelationship(
            catalog.catalog.relations,
            primaryRelation,
            item.right_relation,
            index + 1,
          );
        }),
    );
  }

  function changePrimaryRelation(relation: string) {
    if (!catalog || relation === primaryRelation) {
      return;
    }
    const nextRelations = [
      relation,
      ...selectedRelations.filter((item) => item !== relation),
    ];
    setPrimaryRelation(relation);
    setSelectedRelations(nextRelations);
    setRelationships(
      nextRelations.slice(1).map((rightRelation, index) =>
        suggestedRelationship(
          catalog.catalog.relations,
          relation,
          rightRelation,
          index + 1,
        ),
      ),
    );
  }

  function updateRelationship(
    index: number,
    update: Partial<SemanticRelationship>,
  ) {
    if (!catalog) {
      return;
    }
    setRelationships((current) =>
      current.map((item, itemIndex) => {
        if (itemIndex !== index) {
          return item;
        }
        if (
          update.left_relation &&
          update.left_relation !== item.left_relation
        ) {
          return {
            ...suggestedRelationship(
              catalog.catalog.relations,
              update.left_relation,
              item.right_relation,
              index + 1,
            ),
            join_type: item.join_type,
          };
        }
        return { ...item, ...update };
      }),
    );
  }

  async function confirmBinding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !catalog ||
      !primaryRelation ||
      !selectedRelations.length ||
      !domainId.trim() ||
      busy
    ) {
      return;
    }
    const relations = selectedRelations
      .map((relation) =>
        catalog.catalog.relations.find((item) => item.relation === relation),
      )
      .filter((relation): relation is CatalogRelation => Boolean(relation));
    if (
      relations.length !== selectedRelations.length ||
      relationships.length !== Math.max(0, selectedRelations.length - 1)
    ) {
      setError("请先完成所有数据表的关联配置");
      return;
    }
    const mappings: SemanticFieldMapping[] = relations.flatMap((relation) =>
      relation.columns.map((column) => {
        const key = mappingKey(relation.relation, column.name);
        return {
          logical_ref: logicalRefs[key]?.trim() ?? "",
          physical_relation: relation.relation,
          physical_column: column.name,
          ...compactSemanticFieldMetadata(fieldMetadata[key]),
        };
      }),
    );
    if (mappings.some((mapping) => !mapping.logical_ref)) {
      setError("逻辑字段名不能为空");
      return;
    }
    const metricError = semanticMetricError(
      metrics,
      mappings.map((mapping) => mapping.logical_ref),
    );
    if (metricError) {
      setError(metricError);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const draft = await api.createDataSourceBinding(selectedId, {
        domain_id: domainId.trim(),
        mappings,
        metrics: compactSemanticMetrics(metrics),
        primary_relation: primaryRelation,
        relationships,
      });
      const activated = await api.activateDataSourceBinding(
        selectedId,
        draft.binding_id,
      );
      const refreshed = await api.listDataSourceBindings(selectedId);
      setBindings(refreshed.items);
      onBindingSelect(activated);
      setJustActivated(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "语义绑定创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function activateExisting(bindingId: string) {
    if (busy) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const activated = await api.activateDataSourceBinding(
        selectedId,
        bindingId,
      );
      const refreshed = await api.listDataSourceBindings(selectedId);
      setBindings(refreshed.items);
      onBindingSelect(activated);
      setJustActivated(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "语义绑定激活失败");
    } finally {
      setBusy(false);
    }
  }

  async function deleteDataSource() {
    if (!deleteCandidate || busy) {
      return;
    }
    const source = deleteCandidate;
    setBusy(true);
    setError("");
    try {
      await api.deleteDataSource(source.source_id);
      const refreshed = await onRefresh();
      if (selectedBinding?.source_id === source.source_id) {
        onBindingSelect(null);
      }
      if (selectedId === source.source_id) {
        const nextSource = refreshed[0] ?? null;
        setSelectedId(nextSource?.source_id ?? "");
        setCatalog(null);
        setBindings([]);
        setRelationName("");
        setSelectedRelations([]);
        setPrimaryRelation("");
        setRelationships([]);
      }
      setJustActivated(false);
      setDeleteCandidate(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "数据源删除失败");
    } finally {
      setBusy(false);
    }
  }

  const selectedSource = sources.find((source) => source.source_id === selectedId) ?? null;
  const activeBinding = selectPreferredActiveBinding(bindings, selectedBinding);
  const currentStep = activeBinding ? 4 : catalog ? 3 : selectedSource ? 2 : 1;
  const selectedRelation =
    catalog?.catalog.relations.find((relation) => relation.relation === relationName) ??
    null;
  const selectedCatalogRelations =
    catalog?.catalog.relations.filter((relation) =>
      selectedRelations.includes(relation.relation),
    ) ?? [];
  const semanticEditorFields = selectedCatalogRelations.flatMap((relation) =>
    relation.columns.map((column) => {
      const key = mappingKey(relation.relation, column.name);
      return {
        key,
        logicalRef: logicalRefs[key] ?? "",
        sourceLabel: `${relation.relation}.${column.name}`,
      };
    }),
  );

  return (
    <section className="datasource-page" aria-label="数据源管理">
      <header className="datasource-page-header">
        <div className="datasource-page-title">
          <button className="icon-button" type="button" onClick={onClose} aria-label="返回分析">
            <ArrowLeft size={20} />
          </button>
          <div>
            <h1>连接数据源</h1>
            <p>导入数据、核对目录并激活可用于分析的语义范围。</p>
          </div>
        </div>
        <button className="secondary-action datasource-return" type="button" onClick={onClose}>
          返回分析
          <ArrowRight size={16} />
        </button>
      </header>

      <ol className="datasource-steps" aria-label="数据源配置进度">
        {[
          ["导入或选择", "选择需要分析的数据"],
          ["检查目录", "确认表和字段结构"],
          ["组合与映射", "配置表关联和业务字段"],
          ["激活完成", "用于后续分析会话"],
        ].map(([label, detail], index) => {
          const step = index + 1;
          const complete = currentStep > step;
          const active = currentStep === step;
          return (
            <li
              key={label}
              className={complete ? "complete" : active ? "active" : "waiting"}
              aria-current={active ? "step" : undefined}
            >
              <span>{complete ? <Check size={14} /> : step}</span>
              <div>
                <strong>{label}</strong>
                <small>{detail}</small>
              </div>
            </li>
          );
        })}
      </ol>

      {error && (
        <div className="datasource-error" role="alert">
          <AlertTriangle size={17} />
          <div>
            <strong>数据源操作未完成</strong>
            <span>{error}</span>
          </div>
        </div>
      )}

      {justActivated && activeBinding && (
        <div className="datasource-success" role="status">
          <CheckCircle2 size={19} />
          <div>
            <strong>语义范围已激活</strong>
            <span>数据源现在可以用于规划、预览和执行分析。</span>
          </div>
          <button type="button" onClick={onClose}>开始分析</button>
        </div>
      )}

      <div className="datasource-page-body">
        <aside className="datasource-control">
          <form className="datasource-upload" onSubmit={upload}>
            <div className="datasource-section-heading">
              <Upload size={18} />
              <div>
                <h2>导入文件</h2>
                <p>文件会生成不可变的只读快照。</p>
              </div>
            </div>
            <label>
              <span>数据源名称</span>
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如：销售数据"
                disabled={busy}
              />
            </label>
            <label className="datasource-file-picker">
              <Upload size={18} />
              <span>
                <strong>{files.length ? `已选择 ${files.length} 个文件` : "选择数据文件"}</strong>
                <small>CSV、XLSX 可多选；或单个 SQLite</small>
              </span>
              <input
                type="file"
                multiple
                accept=".csv,.xlsx,.db,.sqlite,.sqlite3"
                onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
                disabled={busy}
              />
            </label>
            {files.length > 0 && (
              <ul className="selected-files">
                {files.map((file) => (
                  <li key={`${file.name}-${file.size}`}>
                    <FileCheck2 size={14} />
                    <span>{file.name}</span>
                  </li>
                ))}
              </ul>
            )}
            <button
              className="datasource-primary"
              type="submit"
              disabled={busy || !name.trim() || !files.length}
            >
              {busy ? <Loader2 className="spin" size={16} /> : <Upload size={16} />}
              {busy ? "正在读取" : "上传并读取"}
            </button>
          </form>

          <div className="registered-sources">
            <div className="datasource-section-heading compact">
              <Database size={17} />
              <div>
                <h2>已注册数据源</h2>
                <p>{sources.length} 个</p>
              </div>
            </div>
            {sources.map((source) => (
              <div className="datasource-list-row" key={source.source_id}>
                <button
                  type="button"
                  className={`datasource-list-item ${
                    selectedId === source.source_id ? "active" : ""
                  }`}
                  onClick={() => setSelectedId(source.source_id)}
                >
                  {source.kind === "sqlite" ? (
                    <Database size={17} />
                  ) : (
                    <FileSpreadsheet size={17} />
                  )}
                  <span>
                    <strong>{source.name}</strong>
                    <small>
                      {source.kind.toUpperCase()} · v{source.active_snapshot_version}
                    </small>
                  </span>
                  {source.status === "ready" && <CheckCircle2 size={15} />}
                </button>
                <button
                  className="datasource-delete-trigger"
                  type="button"
                  aria-label={`删除数据源 ${source.name}`}
                  title="删除数据源"
                  disabled={busy}
                  onClick={() => setDeleteCandidate(source)}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
            {!sources.length && (
              <div className="datasource-empty">
                <Database size={21} />
                <span>还没有数据源</span>
              </div>
            )}
          </div>
        </aside>

        <main className="datasource-workspace">
          {catalog && selectedSource ? (
            <>
              <section className="catalog-overview">
                <div className="catalog-title-row">
                  <div>
                    <span className="source-kind">{selectedSource.kind.toUpperCase()}</span>
                    <h2>{selectedSource.name}</h2>
                    <p>
                      快照 v{catalog.version} · {catalog.catalog.relations.length} 张表 ·
                      只读
                    </p>
                  </div>
                  <span className="status-stamp success">
                    <ShieldCheck size={14} />
                    {activeBinding ? "语义范围已激活" : "目录已读取"}
                  </span>
                </div>
                <dl className="catalog-facts">
                  <div><dt>数据源 ID</dt><dd>{selectedSource.source_id}</dd></div>
                  <div><dt>目录指纹</dt><dd title={catalog.fingerprint}>{shortFingerprint(catalog.fingerprint)}</dd></div>
                  <div><dt>状态</dt><dd>{selectedSource.status === "ready" ? "可用" : selectedSource.status}</dd></div>
                </dl>
              </section>

              <section className="catalog-browser">
                <div className="catalog-relation-list">
                  <div className="datasource-section-heading compact">
                    <Table2 size={17} />
                    <div>
                      <h2>数据目录</h2>
                      <p>选择表查看字段</p>
                    </div>
                  </div>
                  {catalog.catalog.relations.map((relation) => (
                    <button
                      key={relation.relation}
                      type="button"
                      className={relationName === relation.relation ? "active" : ""}
                      onClick={() => setRelationName(relation.relation)}
                    >
                      <span>
                        <strong>{relation.relation}</strong>
                        <small>{relation.columns.length} 个字段</small>
                      </span>
                      <span className="catalog-relation-meta">
                        {selectedRelations.includes(relation.relation) && (
                          <small className="included">已加入</small>
                        )}
                        {relation.estimated_rows !== null && (
                          <small>{relation.estimated_rows.toLocaleString("zh-CN")} 行</small>
                        )}
                      </span>
                    </button>
                  ))}
                </div>

                <div className="catalog-column-view">
                  {selectedRelation ? (
                    <>
                      <div className="column-view-heading">
                        <div>
                          <h3>{selectedRelation.relation}</h3>
                          <p>{selectedRelation.columns.length} 个字段</p>
                        </div>
                        <Layers3 size={18} />
                      </div>
                      <div className="column-table" role="table" aria-label="字段目录">
                        <div className="column-table-head" role="row">
                          <span role="columnheader">物理字段</span>
                          <span role="columnheader">推断类型</span>
                          <span role="columnheader">可空</span>
                        </div>
                        {selectedRelation.columns.map((column) => (
                          <div role="row" key={column.name}>
                            <code role="cell">{column.name}</code>
                            <span role="cell">{column.data_type}</span>
                            <span role="cell">{column.nullable ? "是" : "否"}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  ) : (
                    <div className="datasource-empty">选择一张表查看字段</div>
                  )}
                </div>
              </section>

              <section className="semantic-section">
                <div className="semantic-section-copy">
                  <Waypoints size={21} />
                  <div>
                    <h2>组合数据集与语义</h2>
                    <p>选择主表，逐张加入关联表；查询时仅连接问题实际需要的数据表。</p>
                  </div>
                </div>
                {selectedId && <RelationshipGraphEditor api={api} sourceId={selectedId} binding={isGraphBinding(activeBinding) ? activeBinding : null} onActivated={(binding) => { setBindings((current) => [...current.filter((item) => item.binding_id !== binding.binding_id), binding]); onBindingSelect(binding); setJustActivated(true); }} />}
                {isGraphBinding(activeBinding) ? (
                  <div className="single-table-note" role="status">
                    <ShieldCheck size={16} />
                    当前分析范围使用已激活的 v2 关系图。旧版线性组合编辑器已停用，
                    请在上方关系图中维护跨表路径。
                  </div>
                ) : (
                <form className="datasource-binding-form" onSubmit={confirmBinding}>
                  <div className="datasource-binding-controls">
                    <label>
                      <span>业务域</span>
                      <input
                        value={domainId}
                        onChange={(event) => setDomainId(event.target.value)}
                        disabled={busy}
                      />
                    </label>
                    <label>
                      <span>主表</span>
                      <select
                        value={primaryRelation}
                        onChange={(event) =>
                          changePrimaryRelation(event.target.value)
                        }
                        disabled={busy}
                      >
                        {selectedRelations.map((relation) => (
                          <option key={relation} value={relation}>
                            {relation}
                          </option>
                        ))}
                      </select>
                      <small>更换主表会按新方向重新推荐关联键</small>
                    </label>
                  </div>

                  <div className="dataset-composer">
                    <div className="dataset-composer-heading">
                      <div>
                        <GitMerge size={18} />
                        <span>
                          <strong>数据表组合</strong>
                          <small>
                            {selectedRelations.length} 张表 · {relationships.length} 条关联
                          </small>
                        </span>
                      </div>
                      <span className={relationships.length ? "ready" : ""}>
                        {selectedRelations.length > 1 ? "关联已配置" : "单表数据集"}
                      </span>
                    </div>

                    <div className="dataset-table-pool" aria-label="可加入数据集的数据表">
                      {catalog.catalog.relations.map((relation) => {
                        const included = selectedRelations.includes(relation.relation);
                        const isPrimary = relation.relation === primaryRelation;
                        return (
                          <button
                            key={relation.relation}
                            type="button"
                            className={included ? "included" : ""}
                            onClick={() =>
                              included
                                ? removeRelationFromDataset(relation.relation)
                                : addRelationToDataset(relation.relation)
                            }
                            disabled={busy || isPrimary}
                            aria-pressed={included}
                          >
                            {included ? <Check size={14} /> : <Plus size={14} />}
                            <span>
                              <strong>{relation.relation}</strong>
                              <small>
                                {isPrimary
                                  ? "主表"
                                  : included
                                    ? "点击移除"
                                    : `${relation.columns.length} 个字段`}
                              </small>
                            </span>
                            {included && !isPrimary && <X size={13} />}
                          </button>
                        );
                      })}
                    </div>

                    {relationships.length > 0 ? (
                      <div className="relationship-list">
                        <div className="relationship-list-heading">
                          <span>关联路径</span>
                          <small>每张新增表必须连接到此前已加入的数据表</small>
                        </div>
                        {relationships.map((relationship, index) => {
                          const leftRelation = catalog.catalog.relations.find(
                            (item) => item.relation === relationship.left_relation,
                          );
                          const rightRelation = catalog.catalog.relations.find(
                            (item) => item.relation === relationship.right_relation,
                          );
                          return (
                            <div
                              className="relationship-row"
                              key={relationship.relationship_id}
                            >
                              <span className="relationship-index">{index + 1}</span>
                              <label>
                                <span>已连接表</span>
                                <select
                                  aria-label={`关联 ${index + 1} 的左表`}
                                  value={relationship.left_relation}
                                  onChange={(event) =>
                                    updateRelationship(index, {
                                      left_relation: event.target.value,
                                    })
                                  }
                                  disabled={busy}
                                >
                                  {selectedRelations
                                    .slice(0, index + 1)
                                    .map((relation) => (
                                      <option key={relation} value={relation}>
                                        {relation}
                                      </option>
                                    ))}
                                </select>
                              </label>
                              <label>
                                <span>关联键</span>
                                <select
                                  aria-label={`关联 ${index + 1} 的左字段`}
                                  value={relationship.left_column}
                                  onChange={(event) =>
                                    updateRelationship(index, {
                                      left_column: event.target.value,
                                    })
                                  }
                                  disabled={busy}
                                >
                                  {leftRelation?.columns.map((column) => (
                                    <option key={column.name} value={column.name}>
                                      {column.name}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <label className="join-type-control">
                                <span>连接方式</span>
                                <select
                                  aria-label={`关联 ${index + 1} 的连接方式`}
                                  value={relationship.join_type}
                                  onChange={(event) =>
                                    updateRelationship(index, {
                                      join_type: event.target.value as
                                        | "inner"
                                        | "left",
                                    })
                                  }
                                  disabled={busy}
                                >
                                  <option value="inner">内连接</option>
                                  <option value="left">左连接</option>
                                </select>
                              </label>
                              <div className="relationship-target">
                                <span>新增表</span>
                                <code>{relationship.right_relation}</code>
                              </div>
                              <label>
                                <span>关联键</span>
                                <select
                                  aria-label={`关联 ${index + 1} 的右字段`}
                                  value={relationship.right_column}
                                  onChange={(event) =>
                                    updateRelationship(index, {
                                      right_column: event.target.value,
                                    })
                                  }
                                  disabled={busy}
                                >
                                  {rightRelation?.columns.map((column) => (
                                    <option key={column.name} value={column.name}>
                                      {column.name}
                                    </option>
                                  ))}
                                </select>
                              </label>
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="single-table-note">
                        <Table2 size={16} />
                        当前是单表数据集。加入第二张表后即可配置关联键。
                      </div>
                    )}
                  </div>

                  <div className="mapping-table">
                    <div className="mapping-table-head">
                      <span>数据表</span>
                      <span>物理字段</span>
                      <span>逻辑字段</span>
                    </div>
                    {selectedCatalogRelations.map((relation) => (
                      <div className="mapping-relation-group" key={relation.relation}>
                        {relation.columns.map((column, columnIndex) => {
                          const key = mappingKey(relation.relation, column.name);
                          return (
                            <label key={key}>
                              <code className="mapping-relation-name">
                                {columnIndex === 0 ? relation.relation : ""}
                              </code>
                              <code>{column.name}</code>
                              <input
                                aria-label={`${relation.relation}.${column.name} 的逻辑字段`}
                                value={logicalRefs[key] ?? ""}
                                onChange={(event) =>
                                  setLogicalRefs((current) => ({
                                    ...current,
                                    [key]: event.target.value,
                                  }))
                                }
                                disabled={busy}
                              />
                            </label>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                  <SemanticDefinitionEditor
                    fields={semanticEditorFields}
                    metadata={fieldMetadata}
                    onMetadataChange={(fieldKey, value) =>
                      setFieldMetadata((current) => ({
                        ...current,
                        [fieldKey]: value,
                      }))
                    }
                    metrics={metrics}
                    onMetricsChange={setMetrics}
                    disabled={busy}
                  />
                  <div className="binding-actions">
                    <span>
                      {activeBinding
                        ? `当前已激活 v${activeBinding.version} · ${"relationships" in activeBinding ? activeBinding.relationships?.length ?? 0 : "图"} ${"relationships" in activeBinding ? "条关联" : "关系图"}`
                        : `将激活 ${selectedRelations.length} 张表的统一语义范围`}
                    </span>
                    <button
                      className="datasource-primary"
                      type="submit"
                      disabled={
                        busy ||
                        !primaryRelation ||
                        !selectedRelations.length ||
                        !domainId.trim()
                      }
                    >
                      {busy ? <Loader2 className="spin" size={16} /> : <ShieldCheck size={16} />}
                      {activeBinding ? "创建并激活新版本" : "确认并激活"}
                    </button>
                  </div>
                </form>
                )}

                <div className="datasource-binding-history">
                  <div className="history-heading">
                    <h3>绑定版本</h3>
                    <span>{bindings.length}</span>
                  </div>
                  {bindings.map((binding) => (
                    <div key={binding.binding_id}>
                      <span>
                        <strong>v{binding.version}</strong>
                        {binding.domain_id}
                        <small>{bindingStatusLabel(binding.status)}</small>
                      </span>
                      {binding.status === "draft" && (
                        <button
                          type="button"
                          onClick={() => void activateExisting(binding.binding_id)}
                          disabled={busy}
                        >
                          激活
                        </button>
                      )}
                    </div>
                  ))}
                  {!bindings.length && <small className="evidence-empty">尚未创建绑定</small>}
                </div>
              </section>
            </>
          ) : (
            <div className="datasource-catalog-empty">
              <div className="catalog-empty-graphic" aria-hidden="true">
                <Database size={26} />
                <span />
                <Table2 size={24} />
                <span />
                <ShieldCheck size={26} />
              </div>
              <h2>选择或导入一个数据源</h2>
              <p>完成读取后，这里会显示目录、字段和语义映射。</p>
            </div>
          )}
        </main>
      </div>

      {deleteCandidate && (
        <div className="datasource-dialog-backdrop">
          <div
            className="datasource-delete-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-datasource-title"
          >
            <span className="delete-dialog-icon" aria-hidden="true">
              <Trash2 size={20} />
            </span>
            <div>
              <h2 id="delete-datasource-title">删除数据源？</h2>
              <p>
                将永久删除“{deleteCandidate.name}”及其快照、语义绑定和会话关联。
                此操作无法撤销。
              </p>
            </div>
            <div className="delete-dialog-actions">
              <button
                type="button"
                onClick={() => setDeleteCandidate(null)}
                disabled={busy}
              >
                取消
              </button>
              <button
                className="danger"
                type="button"
                onClick={() => void deleteDataSource()}
                disabled={busy}
              >
                {busy ? <Loader2 className="spin" size={14} /> : <Trash2 size={14} />}
                {busy ? "正在删除" : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function mappingKey(relation: string, column: string): string {
  return `${relation}\u0000${column}`;
}

function logicalEntityName(relation: string): string {
  const table = relation.split(".").at(-1) ?? "Table";
  const words = table.split(/[^a-zA-Z0-9]+/).filter(Boolean);
  const entity = words
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join("");
  return entity || "Table";
}

function suggestedRelationship(
  relations: CatalogRelation[],
  leftRelationName: string,
  rightRelationName: string,
  index: number,
): SemanticRelationship {
  const leftRelation = relations.find(
    (relation) => relation.relation === leftRelationName,
  );
  const rightRelation = relations.find(
    (relation) => relation.relation === rightRelationName,
  );
  const rightColumns = new Map(
    (rightRelation?.columns ?? []).map((column) => [
      column.name.toLocaleLowerCase(),
      column.name,
    ]),
  );
  const matchingLeftColumn = (leftRelation?.columns ?? []).find((column) =>
    rightColumns.has(column.name.toLocaleLowerCase()),
  );
  const leftColumn =
    matchingLeftColumn?.name ?? leftRelation?.columns[0]?.name ?? "";
  const rightColumn =
    (matchingLeftColumn &&
      rightColumns.get(matchingLeftColumn.name.toLocaleLowerCase())) ||
    rightRelation?.columns[0]?.name ||
    "";
  const relationshipSuffix = rightRelationName
    .replace(/[^a-zA-Z0-9._:-]+/g, "_")
    .slice(-80);
  return {
    relationship_id: `join_${index}_${relationshipSuffix}`,
    left_relation: leftRelationName,
    left_column: leftColumn,
    right_relation: rightRelationName,
    right_column: rightColumn,
    join_type: "inner",
  };
}

function shortFingerprint(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}
