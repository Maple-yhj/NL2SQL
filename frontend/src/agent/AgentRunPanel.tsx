import { Activity, AlertCircle, CheckCircle2, Loader2, ShieldCheck } from "lucide-react";

import { AgentInputCard } from "./AgentInputCard";
import { AgentPlanView } from "./AgentPlanView";
import type { AgentRunState } from "./agentRunState";

export function AgentRunPanel({
  state,
  busy,
  onResume,
  onCancel,
}: {
  state: AgentRunState;
  busy: boolean;
  onResume: (message: string, selectedChoice?: string) => void;
  onCancel: () => void;
}) {
  if (state.status === "idle") return null;
  const active = ["running", "resuming"].includes(state.status);
  return (
    <section className={`agent-run-panel agent-run-${state.status}`} aria-label="Agent 运行状态">
      <header className="agent-run-heading">
        <span className="agent-run-mark">
          {active ? <Loader2 className="spin" size={16} /> : state.status === "completed" ? <CheckCircle2 size={16} /> : state.status === "waiting" ? <Activity size={16} /> : <AlertCircle size={16} />}
        </span>
        <div>
          <strong>{runStatusLabel(state.status)}</strong>
          <small>{state.runId}</small>
        </div>
        {(state.artifacts.length > 0 || state.evidence.length > 0) && (
          <span className="agent-run-evidence"><ShieldCheck size={13} />{state.evidence.length} 项证据</span>
        )}
      </header>
      {state.plan && (
        <AgentPlanView plan={state.plan} summaries={state.stepSummaries} tools={state.toolCalls} />
      )}
      {state.observations.length > 0 && (
        <div className="agent-observations">
          {state.observations.slice(-3).map((item) => <p key={item.observationId}>{item.summary}</p>)}
        </div>
      )}
      {state.status === "waiting" && state.inputRequest && (
        <AgentInputCard request={state.inputRequest} disabled={busy} onSubmit={onResume} onCancel={onCancel} />
      )}
      {active && (
        <button className="agent-cancel" type="button" disabled={busy && state.status === "resuming"} onClick={onCancel}>取消运行</button>
      )}
    </section>
  );
}

function runStatusLabel(status: AgentRunState["status"]): string {
  return {
    idle: "尚未运行",
    running: "正在执行分析计划",
    waiting: "分析已暂停",
    resuming: "正在恢复分析",
    completed: "分析已完成",
    failed: "分析未完成",
    cancelled: "分析已取消",
  }[status];
}
