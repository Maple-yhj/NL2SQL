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
  const evidenceComplete = Boolean(
    runStage === "complete" ||
      metadata.sql ||
      metadata.rows?.length ||
      metadata.trace?.length ||
      hasTerminalMessage,
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
            : evidenceComplete
              ? "证据可查看"
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
      detail: failed ? "需要处理" : hasTerminalMessage ? "回答已生成" : "等待生成",
      status: failed ? "error" : hasTerminalMessage ? "complete" : "waiting",
    },
  ];
}
