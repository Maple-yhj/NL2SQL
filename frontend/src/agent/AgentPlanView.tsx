import type { AnalysisPlan, AnalysisStepSummary } from "../types";
import { AgentStepItem } from "./AgentStepItem";
import type { AgentToolCallView } from "./agentRunState";

export function AgentPlanView({
  plan,
  summaries,
  tools,
}: {
  plan: AnalysisPlan;
  summaries: AnalysisStepSummary[];
  tools: AgentToolCallView[];
}) {
  return (
    <section className="agent-plan" aria-label="分析计划">
      <header>
        <strong>分析计划</strong>
        <span>修订 {plan.revision}</span>
      </header>
      <ol>
        {plan.steps.map((step, index) => (
          <AgentStepItem
            key={step.step_id}
            step={step}
            summary={summaries.find((item) => item.step_id === step.step_id)}
            tools={index === plan.steps.length - 1 ? tools.slice(-1) : []}
          />
        ))}
      </ol>
    </section>
  );
}
