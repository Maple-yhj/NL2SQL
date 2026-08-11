import { Plus, Trash2 } from "lucide-react";

import type {
  SemanticFieldMetadata,
  SemanticMetricDefinition,
  SemanticRole,
} from "./types";

export interface SemanticEditorField {
  key: string;
  logicalRef: string;
  sourceLabel: string;
}

interface SemanticDefinitionEditorProps {
  fields: SemanticEditorField[];
  metadata: Record<string, SemanticFieldMetadata>;
  onMetadataChange: (
    fieldKey: string,
    value: SemanticFieldMetadata,
  ) => void;
  metrics: SemanticMetricDefinition[];
  onMetricsChange: (metrics: SemanticMetricDefinition[]) => void;
  disabled?: boolean;
}

const semanticRoles: Array<{ value: SemanticRole; label: string }> = [
  { value: "identifier", label: "标识符" },
  { value: "dimension", label: "维度" },
  { value: "measure", label: "度量" },
  { value: "time", label: "时间" },
  { value: "status", label: "状态" },
  { value: "attribute", label: "属性" },
];

const metricOperations: Array<{
  value: SemanticMetricDefinition["operation"];
  label: string;
}> = [
  { value: "count", label: "行数 COUNT" },
  { value: "count_distinct", label: "去重计数 COUNT DISTINCT" },
  { value: "sum", label: "求和 SUM" },
  { value: "avg", label: "平均 AVG" },
  { value: "min", label: "最小值 MIN" },
  { value: "max", label: "最大值 MAX" },
  { value: "median", label: "中位数 MEDIAN" },
];

const cleanText = (value: string | null | undefined) => value?.trim() || undefined;

export function compactSemanticFieldMetadata(
  value: SemanticFieldMetadata | undefined,
): SemanticFieldMetadata {
  if (!value) return {};
  const synonyms = (value.synonyms ?? [])
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    ...(cleanText(value.display_name)
      ? { display_name: cleanText(value.display_name) }
      : {}),
    ...(cleanText(value.description)
      ? { description: cleanText(value.description) }
      : {}),
    ...(value.semantic_role ? { semantic_role: value.semantic_role } : {}),
    ...(cleanText(value.entity) ? { entity: cleanText(value.entity) } : {}),
    ...(cleanText(value.grain) ? { grain: cleanText(value.grain) } : {}),
    ...(cleanText(value.unit) ? { unit: cleanText(value.unit) } : {}),
    ...(cleanText(value.lifecycle_stage)
      ? { lifecycle_stage: cleanText(value.lifecycle_stage) }
      : {}),
    ...(synonyms.length ? { synonyms } : {}),
  };
}

export function semanticMetricError(
  metrics: SemanticMetricDefinition[],
  fieldRefs: string[],
): string | null {
  const refs = new Set<string>();
  const fields = new Set(fieldRefs);
  for (const metric of metrics) {
    const metricRef = metric.metric_ref.trim();
    if (!metricRef || !metric.display_name.trim() || !metric.description.trim()) {
      return "业务指标的引用、显示名和口径说明不能为空";
    }
    if (refs.has(metricRef)) return "业务指标引用不能重复";
    refs.add(metricRef);
    if (metric.operation !== "count") {
      if (!metric.field_ref?.trim()) return "非行数指标必须选择来源逻辑字段";
      if (!fields.has(metric.field_ref.trim())) {
        return "业务指标引用了当前绑定范围之外的逻辑字段";
      }
    }
  }
  return null;
}

export function compactSemanticMetrics(
  metrics: SemanticMetricDefinition[],
): SemanticMetricDefinition[] {
  return metrics.map((metric) => {
    const synonyms = (metric.synonyms ?? [])
      .map((item) => item.trim())
      .filter(Boolean);
    return {
      metric_ref: metric.metric_ref.trim(),
      display_name: metric.display_name.trim(),
      description: metric.description.trim(),
      operation: metric.operation,
      ...(metric.field_ref?.trim() ? { field_ref: metric.field_ref.trim() } : {}),
      ...(cleanText(metric.unit) ? { unit: cleanText(metric.unit) } : {}),
      ...(cleanText(metric.grain) ? { grain: cleanText(metric.grain) } : {}),
      ...(synonyms.length ? { synonyms } : {}),
    };
  });
}

function emptyMetric(index: number): SemanticMetricDefinition {
  return {
    metric_ref: `dataset.metrics.metric_${index + 1}`,
    display_name: "",
    description: "",
    operation: "count",
    field_ref: null,
    unit: null,
    grain: null,
    synonyms: [],
  };
}

function splitSynonyms(value: string): string[] {
  return value
    .split(/[，,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function SemanticDefinitionEditor({
  fields,
  metadata,
  onMetadataChange,
  metrics,
  onMetricsChange,
  disabled = false,
}: SemanticDefinitionEditorProps) {
  const updateMetric = (
    index: number,
    update: Partial<SemanticMetricDefinition>,
  ) => {
    onMetricsChange(
      metrics.map((metric, metricIndex) =>
        metricIndex === index ? { ...metric, ...update } : metric,
      ),
    );
  };

  return (
    <details className="semantic-definition-editor">
      <summary>
        字段业务语义与指标口径（可选）
        <small>用于回答字段含义并保护收入、利润等高风险口径</small>
      </summary>

      <div className="semantic-definition-fields">
        <h4>字段业务语义</h4>
        <p>只需为需要业务解释的字段补充信息；未填写内容不会写入绑定。</p>
        {fields.map((field) => {
          const value = metadata[field.key] ?? {};
          const update = (change: Partial<SemanticFieldMetadata>) =>
            onMetadataChange(field.key, { ...value, ...change });
          return (
            <details className="semantic-field-definition" key={field.key}>
              <summary>
                <code>{field.logicalRef}</code>
                <small>{field.sourceLabel}</small>
              </summary>
              <div className="semantic-field-grid">
                <label>
                  <span>业务显示名</span>
                  <input
                    value={value.display_name ?? ""}
                    onChange={(event) => update({ display_name: event.target.value })}
                    disabled={disabled}
                  />
                </label>
                <label>
                  <span>语义角色</span>
                  <select
                    value={value.semantic_role ?? ""}
                    onChange={(event) =>
                      update({
                        semantic_role:
                          (event.target.value as SemanticRole) || null,
                      })
                    }
                    disabled={disabled}
                  >
                    <option value="">未指定</option>
                    {semanticRoles.map((role) => (
                      <option key={role.value} value={role.value}>
                        {role.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="wide">
                  <span>业务定义</span>
                  <textarea
                    value={value.description ?? ""}
                    onChange={(event) => update({ description: event.target.value })}
                    disabled={disabled}
                    placeholder="说明字段代表什么、适用范围及与相近字段的区别"
                  />
                </label>
                <label>
                  <span>业务实体</span>
                  <input
                    value={value.entity ?? ""}
                    onChange={(event) => update({ entity: event.target.value })}
                    disabled={disabled}
                  />
                </label>
                <label>
                  <span>数据粒度</span>
                  <input
                    value={value.grain ?? ""}
                    onChange={(event) => update({ grain: event.target.value })}
                    disabled={disabled}
                  />
                </label>
                <label>
                  <span>单位</span>
                  <input
                    value={value.unit ?? ""}
                    onChange={(event) => update({ unit: event.target.value })}
                    disabled={disabled}
                  />
                </label>
                <label>
                  <span>生命周期阶段</span>
                  <input
                    value={value.lifecycle_stage ?? ""}
                    onChange={(event) =>
                      update({ lifecycle_stage: event.target.value })
                    }
                    disabled={disabled}
                  />
                </label>
                <label className="wide">
                  <span>同义词（逗号分隔）</span>
                  <input
                    value={(value.synonyms ?? []).join(", ")}
                    onChange={(event) =>
                      update({ synonyms: splitSynonyms(event.target.value) })
                    }
                    disabled={disabled}
                  />
                </label>
              </div>
            </details>
          );
        })}
      </div>

      <div className="semantic-metric-definitions">
        <div className="semantic-metric-heading">
          <div>
            <h4>受治理业务指标</h4>
            <p>显式定义来源字段、聚合方式、单位、粒度与业务口径。</p>
          </div>
          <button
            type="button"
            onClick={() => onMetricsChange([...metrics, emptyMetric(metrics.length)])}
            disabled={disabled}
          >
            <Plus size={14} />
            添加指标
          </button>
        </div>
        {metrics.map((metric, index) => (
          <div className="semantic-metric-card" key={`${index}-${metric.metric_ref}`}>
            <div className="semantic-metric-card-heading">
              <strong>指标 {index + 1}</strong>
              <button
                type="button"
                aria-label={`删除指标 ${index + 1}`}
                onClick={() =>
                  onMetricsChange(metrics.filter((_, itemIndex) => itemIndex !== index))
                }
                disabled={disabled}
              >
                <Trash2 size={14} />
              </button>
            </div>
            <div className="semantic-field-grid">
              <label>
                <span>指标引用</span>
                <input
                  value={metric.metric_ref}
                  onChange={(event) => updateMetric(index, { metric_ref: event.target.value })}
                  disabled={disabled}
                />
              </label>
              <label>
                <span>显示名</span>
                <input
                  value={metric.display_name}
                  onChange={(event) => updateMetric(index, { display_name: event.target.value })}
                  disabled={disabled}
                />
              </label>
              <label>
                <span>聚合方式</span>
                <select
                  value={metric.operation}
                  onChange={(event) =>
                    updateMetric(index, {
                      operation: event.target.value as SemanticMetricDefinition["operation"],
                      ...(event.target.value === "count" ? { field_ref: null } : {}),
                    })
                  }
                  disabled={disabled}
                >
                  {metricOperations.map((operation) => (
                    <option key={operation.value} value={operation.value}>
                      {operation.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>来源逻辑字段</span>
                <select
                  value={metric.field_ref ?? ""}
                  onChange={(event) =>
                    updateMetric(index, { field_ref: event.target.value || null })
                  }
                  disabled={disabled || metric.operation === "count"}
                >
                  <option value="">
                    {metric.operation === "count" ? "不需要（统计行数）" : "请选择"}
                  </option>
                  {fields.map((field) => (
                    <option key={field.key} value={field.logicalRef}>
                      {field.logicalRef}
                    </option>
                  ))}
                </select>
              </label>
              <label className="wide">
                <span>口径说明</span>
                <textarea
                  value={metric.description}
                  onChange={(event) => updateMetric(index, { description: event.target.value })}
                  disabled={disabled}
                  placeholder="说明包含/排除项、计算口径和适用范围"
                />
              </label>
              <label>
                <span>单位</span>
                <input
                  value={metric.unit ?? ""}
                  onChange={(event) => updateMetric(index, { unit: event.target.value })}
                  disabled={disabled}
                />
              </label>
              <label>
                <span>粒度</span>
                <input
                  value={metric.grain ?? ""}
                  onChange={(event) => updateMetric(index, { grain: event.target.value })}
                  disabled={disabled}
                />
              </label>
              <label className="wide">
                <span>同义词（逗号分隔）</span>
                <input
                  value={(metric.synonyms ?? []).join(", ")}
                  onChange={(event) =>
                    updateMetric(index, { synonyms: splitSynonyms(event.target.value) })
                  }
                  disabled={disabled}
                />
              </label>
            </div>
          </div>
        ))}
        {!metrics.length && <small>尚未定义业务指标；高风险口径将默认拒绝推断。</small>}
      </div>
    </details>
  );
}
