import type { ChatMessage } from "./types";

export type RunStage = "idle" | "started" | "pinned" | "complete" | "failed";
export type EvidenceStepStatus = "complete" | "active" | "waiting" | "error" | "action";

export interface EvidenceStep {
  id: "dataset" | "semantic" | "query" | "evidence" | "answer";
  label: string;
  detail: string;
  status: EvidenceStepStatus;
}

interface EvidenceRouteInput {
  hasBinding: boolean;
  runStage: RunStage;
  latestAssistant: ChatMessage | null;
}

export function buildEvidenceRoute({
  hasBinding,
  runStage,
  latestAssistant,
}: EvidenceRouteInput): EvidenceStep[] {
  const metadata = latestAssistant?.metadata ?? {};
  const hasTerminalMessage = Boolean(
    latestAssistant &&
      metadata.message_type !== "thinking" &&
      (metadata.answer || latestAssistant.content || metadata.ok !== undefined || metadata.error),
  );
  const failed = runStage === "failed" || Boolean(metadata.error);
  const queryComplete = Boolean(
    runStage === "pinned" ||
      runStage === "complete" ||
      metadata.logical_plan ||
      metadata.sql ||
      hasTerminalMessage,
  );
  const artifacts = metadata.artifacts ?? [];
  const hasResultArtifact = artifacts.some(
    (artifact) => artifact.kind === "query_preview" || artifact.kind === "query_result",
  );
  const isPlanOnly = Boolean(
    hasTerminalMessage &&
      !hasResultArtifact &&
      !metadata.rows?.length &&
      !metadata.evidence?.length &&
      (metadata.limitations?.some((item) =>
        /plan mode|规划模式/i.test(item),
      ) || artifacts.some((artifact) => artifact.kind === "prepared_query")),
  );
  const hasEvidence = Boolean(
    metadata.evidence?.length,
  );
  const evidenceComplete = Boolean(
    !failed && hasEvidence,
  );

  return [
    {
      id: "dataset",
      label: "数据集",
      detail: hasBinding ? "已选择数据" : "等待选择",
      status: hasBinding ? "complete" : "action",
    },
    {
      id: "semantic",
      label: "语义范围",
      detail: hasBinding ? "绑定已激活" : "等待激活",
      status: hasBinding ? "complete" : "waiting",
    },
    {
      id: "query",
      label: "查询",
      detail:
        runStage === "started"
          ? "正在规划"
          : failed
            ? "运行失败"
            : isPlanOnly
              ? "查询计划已生成"
            : queryComplete
              ? "查询已生成"
              : "等待提问",
      status: failed
        ? "error"
        : runStage === "started"
          ? "active"
          : queryComplete
            ? "complete"
            : "waiting",
    },
    {
      id: "evidence",
      label: "证据",
      detail:
        runStage === "pinned"
          ? "正在核验"
          : failed
            ? "核验中止"
            : isPlanOnly
              ? "规划模式未执行"
            : evidenceComplete
              ? "证据可查看"
              : hasTerminalMessage
                ? "未生成证据"
                : "等待结果",
      status: failed
        ? "error"
        : runStage === "pinned"
          ? "active"
          : evidenceComplete
            ? "complete"
            : "waiting",
    },
    {
      id: "answer",
      label: "回答",
      detail: failed
        ? "需要处理"
        : isPlanOnly
          ? "计划已生成"
          : hasTerminalMessage
            ? "回答已生成"
            : "等待生成",
      status: failed ? "error" : hasTerminalMessage ? "complete" : "waiting",
    },
  ];
}

export function buildEvidenceBadge(message: ChatMessage): string | null {
  const metadata = message.metadata;
  const evidenceCount = metadata.evidence?.length ?? 0;
  const rowCount = metadata.rows?.length ?? 0;
  const traceCount = metadata.trace?.length ?? 0;
  const hasRoute = Boolean(
    metadata.logical_plan ||
      metadata.sql ||
      rowCount ||
      traceCount ||
      metadata.version_pins ||
      metadata.analysis_steps?.length ||
      metadata.artifacts?.length ||
      evidenceCount,
  );
  if (!hasRoute) return null;
  if (evidenceCount) return `${evidenceCount} 项证据`;
  if (rowCount) return `${rowCount} 条结果`;
  if (traceCount) return `${traceCount} 个运行节点`;
  if (metadata.error || metadata.ok === false) return "无有效证据";
  return "运行记录";
}
