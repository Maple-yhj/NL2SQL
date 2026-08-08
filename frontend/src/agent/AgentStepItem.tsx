import { Check, Circle, Loader2, ShieldAlert, Wrench } from "lucide-react";

import type { AnalysisStep, AnalysisStepSummary } from "../types";
import type { AgentToolCallView } from "./agentRunState";

const FALLBACK_TOOL_NAMES: Record<string, string> = {
  "catalog.inspect": "检查数据目录",
  "semantic.inspect": "检查语义范围",
  "relationship.route": "解析关系路径",
  "query.compile": "编译受治理查询",
  "query.explain": "评估查询计划",
  "query.preview": "预览查询结果",
  "query.execute": "执行受治理查询",
  "data.profile": "分析数据分布",
  "compute.restricted": "执行受限计算",
  "chart.build": "生成图表",
  "evidence.collect": "整理证据",
};

export function toolBusinessName(toolName: string, displayName: string): string {
  return displayName.trim() || FALLBACK_TOOL_NAMES[toolName] || "分析工具";
}

export function AgentStepItem({
  step,
  summary,
  tools,
}: {
  step: AnalysisStep;
  summary?: AnalysisStepSummary;
  tools: AgentToolCallView[];
}) {
  const status = summary?.status ?? step.status;
  return (
    <li className={`agent-step agent-step-${status}`}>
      <span className="agent-step-icon" aria-hidden="true">
        {status === "completed" ? (
          <Check size={14} />
        ) : status === "running" ? (
          <Loader2 className="spin" size={14} />
        ) : status === "failed" || status === "blocked" ? (
          <ShieldAlert size={14} />
        ) : (
          <Circle size={12} />
        )}
      </span>
      <div>
        <strong>{step.objective}</strong>
        <small>{statusLabel(status)}</small>
        {(summary?.tool_names.length || tools.length > 0) && (
          <div className="agent-step-tools">
            {(summary?.tool_names ?? []).map((name) => (
              <span key={name}><Wrench size={11} />{toolBusinessName(name, "")}</span>
            ))}
            {tools.map((tool) => (
              <span key={tool.callId}><Wrench size={11} />{toolBusinessName(tool.toolName, tool.displayName)}</span>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

function statusLabel(status: string): string {
  return {
    pending: "待执行",
    running: "执行中",
    completed: "已完成",
    blocked: "已阻断",
    skipped: "已跳过",
    waiting_input: "等待输入",
    failed: "失败",
  }[status] ?? status;
}
