import {
  AlertTriangle,
  CheckCircle2,
  Database,
  FileSpreadsheet,
  Loader2,
  Table2,
  Upload,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import type { ApiClient } from "./api";
import type {
  DataSource,
  DataSourceCatalog,
  SemanticBinding,
  SemanticFieldMapping,
} from "./types";

interface DataSourcePanelProps {
  api: ApiClient;
  sources: DataSource[];
  selectedBinding: SemanticBinding | null;
  onRefresh: () => Promise<DataSource[]>;
  onBindingSelect: (binding: SemanticBinding | null) => void;
  onClose: () => void;
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
  const [bindings, setBindings] = useState<SemanticBinding[]>([]);
  const [relationName, setRelationName] = useState("");
  const [domainId, setDomainId] = useState("");
  const [logicalRefs, setLogicalRefs] = useState<Record<string, string>>({});
  const [name, setName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

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
          setCatalog(payload);
          setBindings(bindingPayload.items);
          onBindingSelect(
            bindingPayload.items.find((item) => item.status === "active") ?? null,
          );
          const firstRelation = payload.catalog.relations[0];
          setRelationName((current) =>
            payload.catalog.relations.some(
              (relation) => relation.relation === current,
            )
              ? current
              : firstRelation?.relation ?? "",
          );
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

  async function confirmBinding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!catalog || !relationName || !domainId.trim() || busy) {
      return;
    }
    const relation = catalog.catalog.relations.find(
      (item) => item.relation === relationName,
    );
    if (!relation) {
      return;
    }
    const mappings: SemanticFieldMapping[] = relation.columns.map((column) => ({
      logical_ref: logicalRefs[mappingKey(relation.relation, column.name)]?.trim() ?? "",
      physical_relation: relation.relation,
      physical_column: column.name,
    }));
    if (mappings.some((mapping) => !mapping.logical_ref)) {
      setError("逻辑字段名不能为空");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const draft = await api.createDataSourceBinding(selectedId, {
        domain_id: domainId.trim(),
        mappings,
      });
      const activated = await api.activateDataSourceBinding(
        selectedId,
        draft.binding_id,
      );
      const refreshed = await api.listDataSourceBindings(selectedId);
      setBindings(refreshed.items);
      onBindingSelect(activated);
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
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "语义绑定激活失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="datasource-overlay" role="presentation" onMouseDown={onClose}>
      <section
        className="datasource-panel"
        role="dialog"
        aria-modal="true"
        aria-label="数据源管理"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="datasource-panel-header">
          <div>
            <h2>数据源</h2>
            <p>上传文件、查看结构，并为后续语义绑定准备数据。</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭数据源">
            <X size={19} />
          </button>
        </header>

        {error && (
          <div className="datasource-error">
            <AlertTriangle size={16} />
            {error}
          </div>
        )}

        <div className="datasource-panel-body">
          <aside className="datasource-list">
            <form className="datasource-upload" onSubmit={upload}>
              <label>
                <span>显示名称</span>
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="例如：销售数据"
                  disabled={busy}
                />
              </label>
              <label className="datasource-file-picker">
                <Upload size={16} />
                <span>{files.length ? `已选择 ${files.length} 个文件` : "选择数据文件"}</span>
                <input
                  type="file"
                  multiple
                  accept=".csv,.xlsx,.db,.sqlite,.sqlite3"
                  onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
                  disabled={busy}
                />
              </label>
              <div className="datasource-file-help">
                支持 CSV、XLSX 和 SQLite；文件会生成不可变只读快照。
              </div>
              <button
                className="datasource-primary"
                type="submit"
                disabled={busy || !name.trim() || !files.length}
              >
                {busy ? <Loader2 className="spin" size={16} /> : <Upload size={16} />}
                上传并读取
              </button>
            </form>

            <div className="datasource-list-heading">已注册</div>
            {sources.map((source) => (
              <button
                key={source.source_id}
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
            ))}
            {!sources.length && <div className="datasource-empty">暂无数据源</div>}
          </aside>

          <div className="datasource-catalog">
            {catalog ? (
              <>
                <div className="datasource-catalog-heading">
                  <div>
                    <h3>数据目录</h3>
                    <p>
                      {catalog.catalog.relations.length} 张表 · 快照 v{catalog.version}
                    </p>
                  </div>
                  <Table2 size={19} />
                </div>
                <div className="datasource-relations">
                  {catalog.catalog.relations.map((relation) => (
                    <article key={relation.relation} className="datasource-relation">
                      <div className="datasource-relation-title">
                        <strong>{relation.relation}</strong>
                        {relation.estimated_rows !== null && (
                          <span>{relation.estimated_rows} 行</span>
                        )}
                      </div>
                      <div className="datasource-columns">
                        {relation.columns.map((column) => (
                          <div key={column.name}>
                            <span>{column.name}</span>
                            <code>{column.data_type}</code>
                          </div>
                        ))}
                      </div>
                    </article>
                  ))}
                </div>
                <div className="datasource-binding-note">
                  <strong>语义绑定</strong>
                  <span>确认字段映射并激活后，才能用于对话查询。</span>
                </div>
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
                      <span>数据表</span>
                      <select
                        value={relationName}
                        onChange={(event) => setRelationName(event.target.value)}
                        disabled={busy}
                      >
                        {catalog.catalog.relations.map((relation) => (
                          <option key={relation.relation} value={relation.relation}>
                            {relation.relation}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <div className="datasource-mapping-list">
                    {catalog.catalog.relations
                      .find((relation) => relation.relation === relationName)
                      ?.columns.map((column) => {
                        const key = mappingKey(relationName, column.name);
                        return (
                          <label key={key}>
                            <code>{column.name}</code>
                            <input
                              aria-label={`${column.name} 的逻辑字段`}
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
                  <button
                    className="datasource-primary"
                    type="submit"
                    disabled={busy || !relationName || !domainId.trim()}
                  >
                    {busy ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />}
                    确认并激活
                  </button>
                </form>
                <div className="datasource-binding-history">
                  {bindings.map((binding) => (
                    <div key={binding.binding_id}>
                      <span>
                        <strong>v{binding.version}</strong>
                        {binding.domain_id} · {binding.status}
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
                  {!bindings.length && <small>尚未创建绑定</small>}
                </div>
              </>
            ) : (
              <div className="datasource-catalog-empty">
                <Database size={28} />
                <strong>选择一个已就绪的数据源</strong>
                <span>这里会显示表、字段和推断类型。</span>
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
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
