"""Provider-neutral structured planner for the native analysis Agent."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from data_agent.tools.models import ToolSpec

from .models import (
    AgentAction,
    AgentContextSnapshot,
    AgentInputReason,
    AgentInputRequest,
    AgentObservation,
    AnalysisGoal,
    AnalysisPlan,
    AnalysisStep,
    PlannerDecision,
    stable_digest,
)
from .prompts import (
    ModelClient,
    NEXT_ACTION_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    bounded_text,
    build_next_action_prompt,
    build_planner_prompt,
    complete_strict_model,
)


class AnalysisPlanner:
    def __init__(self, model_client: ModelClient, *, max_attempts: int = 2) -> None:
        if not 1 <= max_attempts <= 3:
            raise ValueError("planner max_attempts must be between 1 and 3")
        self._model_client = model_client
        self._max_attempts = max_attempts

    async def decide(
        self,
        *,
        goal: AnalysisGoal,
        context: AgentContextSnapshot,
        current_plan: AnalysisPlan | None,
        observations: Sequence[AgentObservation],
        budget_remaining: Mapping[str, int],
        allowed_tools: Sequence[ToolSpec],
        max_observation_cells: int = 400,
        replan_requested: bool = False,
    ) -> PlannerDecision:
        if current_plan is None and not observations and _needs_metric_clarification(
            goal.original_question
        ):
            return _metric_clarification(goal)
        specs = {spec.name: spec for spec in allowed_tools}
        if (
            current_plan is not None
            and observations
            and observations[-1].status == "succeeded"
            and not replan_requested
            and any(
                step.status not in {"completed", "skipped"}
                for step in current_plan.steps
            )
        ):
            prompt = build_next_action_prompt(
                goal=goal,
                context=context,
                current_plan=current_plan,
                observations=observations,
                budget_remaining=budget_remaining,
                allowed_tools=allowed_tools,
                max_observation_cells=max_observation_cells,
            )

            def validate_action(action: AgentAction) -> None:
                self._validate_action(
                    action,
                    specs=specs,
                    observations=observations,
                )

            action = await complete_strict_model(
                model_client=self._model_client,
                prompt=prompt,
                system=NEXT_ACTION_SYSTEM_PROMPT,
                output_type=AgentAction,
                task="next_analysis_action",
                max_attempts=self._max_attempts,
                validator=validate_action,
            )
            return PlannerDecision(
                plan=current_plan,
                decision="act",
                next_action=action,
                rationale_summary=(
                    "Selected the next governed action for the pending plan step."
                ),
            )
        prompt = build_planner_prompt(
            goal=goal,
            context=context,
            current_plan=current_plan,
            observations=observations,
            budget_remaining=budget_remaining,
            allowed_tools=allowed_tools,
            output_schema=PlannerDecision,
            max_observation_cells=max_observation_cells,
        )

        def validate(decision: PlannerDecision) -> None:
            self._validate_decision(
                self._normalize_initial_decision(decision)
                if current_plan is None
                else decision,
                current_plan=current_plan,
                specs=specs,
            )

        decision = await complete_strict_model(
            model_client=self._model_client,
            prompt=prompt,
            system=PLANNER_SYSTEM_PROMPT,
            output_type=PlannerDecision,
            task="planner_decision",
            max_attempts=self._max_attempts,
            validator=validate,
        )
        return (
            self._normalize_initial_decision(decision)
            if current_plan is None
            else decision
        )

    @staticmethod
    def _normalize_initial_decision(decision: PlannerDecision) -> PlannerDecision:
        """Normalize model-owned lifecycle fields that the runtime owns."""

        plan = decision.plan.model_copy(
            update={
                "revision": 1,
                "steps": tuple(
                    step.model_copy(update={"status": "pending"})
                    for step in decision.plan.steps
                ),
            }
        )
        return decision.model_copy(update={"plan": plan})

    @staticmethod
    def _validate_decision(
        decision: PlannerDecision,
        *,
        current_plan: AnalysisPlan | None,
        specs: Mapping[str, ToolSpec],
    ) -> None:
        if current_plan is None:
            if decision.plan.revision != 1:
                raise ValueError("initial analysis plan revision must be 1")
        elif decision.plan != current_plan and decision.plan.revision != current_plan.revision + 1:
            raise ValueError("a changed analysis plan must increment revision exactly once")
        if decision.next_action is None:
            return
        completed = {
            step.step_id
            for step in decision.plan.steps
            if step.status in {"completed", "skipped"}
        }
        if not any(
            step.status == "pending"
            and set(step.depends_on).issubset(completed)
            for step in decision.plan.steps
        ):
            raise ValueError("planner action requires a runnable pending plan step")
        spec = specs.get(decision.next_action.tool_name)
        if spec is None:
            raise ValueError("planner selected a tool outside the allowed registry view")
        try:
            spec.input_schema.model_validate(decision.next_action.arguments)
        except (TypeError, ValueError) as exc:
            raise ValueError("planner action does not match the allowed tool input schema") from exc

    @staticmethod
    def _validate_action(
        action: AgentAction,
        *,
        specs: Mapping[str, ToolSpec],
        observations: Sequence[AgentObservation],
    ) -> None:
        spec = specs.get(action.tool_name)
        if spec is None:
            raise ValueError("planner selected a tool outside the allowed registry view")
        try:
            spec.input_schema.model_validate(action.arguments)
        except (TypeError, ValueError) as exc:
            raise ValueError("planner action does not match the allowed tool input schema") from exc
        if any(
            observation.status == "succeeded"
            and observation.tool_name == action.tool_name
            and not action.arguments
            for observation in observations
        ):
            raise ValueError("planner repeated a completed metadata inspection")

    @staticmethod
    def rebuild_follow_up_goal(
        *,
        question: str,
        context: AgentContextSnapshot,
        requested_output: str = "grounded analysis answer",
        success_criteria: tuple[str, ...] = (
            "Answer the current question with validated evidence",
        ),
    ) -> AnalysisGoal:
        current_question = bounded_text(question, max_chars=4000).strip()
        if not current_question:
            raise ValueError("follow-up question must not be blank")
        conversation = (
            bounded_text(context.conversation_summary, max_chars=4000).strip()
            if context.conversation_summary
            else ""
        )
        contextualized = (
            f"Conversation summary: {conversation}\nCurrent question: {current_question}"
            if conversation
            else current_question
        )
        return AnalysisGoal(
            original_question=current_question,
            contextualized_question=contextualized,
            requested_output=requested_output,
            success_criteria=success_criteria,
            constraints=(
                "Use only the current datasource pins and validated evidence",
            ),
        )


_AGGREGATE_INTENT = re.compile(
    r"(?:按|每|各|趋势|排名|排行|最高|最低|最多|最少|对比|比较|分布|占比|汇总|统计|分析)"
    r"|\b(?:by|per|trend|rank|ranking|top|bottom|compare|comparison|distribution|"
    r"breakdown|summary|analy[sz]e|analysis)\b",
    re.IGNORECASE,
)
_EXPLICIT_METRIC = re.compile(
    r"(?:金额|销售额|销量|营收|收入|价格|运费|成本|利润|数量|订单数|客户数|产品数|"
    r"记录数|总数|条数|笔数|件数|评分|分数|时长|天数|频次|次数|比率|比例|百分比|转化率|合计|总计|"
    r"平均|均值|最大值|最小值|多少|最多|最少)"
    r"|\b(?:revenue|sales|amount|price|freight|cost|profit|count|number|quantity|"
    r"score|rating|duration|days?|frequency|rate|ratio|percent(?:age)?|average|avg|"
    r"sum|total|how many|maximum|minimum)\b",
    re.IGNORECASE,
)
_DETAIL_INTENT = re.compile(
    r"(?:列出|明细|清单|列表)"
    r"|\b(?:show|list|detail|records?|rows?)\b",
    re.IGNORECASE,
)
_NON_DETAIL_ANALYSIS = re.compile(
    r"(?:趋势|排名|排行|最高|最低|最多|最少|对比|比较|分布|占比|汇总|统计|分析)"
    r"|\b(?:trend|rank|ranking|top|bottom|compare|comparison|distribution|"
    r"breakdown|summary|analy[sz]e|analysis)\b",
    re.IGNORECASE,
)


def _needs_metric_clarification(question: str) -> bool:
    text = question.strip()
    if not text or _EXPLICIT_METRIC.search(text) or not _AGGREGATE_INTENT.search(text):
        return False
    if _DETAIL_INTENT.search(text) and not _NON_DETAIL_ANALYSIS.search(text):
        return False
    return True


def _metric_clarification(goal: AnalysisGoal) -> PlannerDecision:
    digest = stable_digest(
        {
            "question": goal.original_question,
            "kind": "metric_clarification",
        }
    )[:16]
    chinese = re.search(r"[\u3400-\u9fff]", goal.original_question) is not None
    prompt = (
        "这个问题没有明确要计算的指标。请选择指标（例如订单数量、金额合计或平均评分），也可以直接输入其他指标。"
        if chinese
        else "This question does not specify which metric to calculate. Choose a metric "
        "such as record count, total amount, or average score, or enter another metric."
    )
    choices = (
        ("订单/记录数量", "金额合计", "平均值")
        if chinese
        else ("Record count", "Total amount", "Average value")
    )
    plan = AnalysisPlan(
        plan_id=f"metric-clarification-{digest}",
        revision=1,
        steps=(
            AnalysisStep(
                step_id="confirm-metric",
                objective=("确认分析指标" if chinese else "Confirm the analysis metric"),
                status="pending",
                expected_evidence=("confirmed_metric",),
            ),
        ),
        completion_criteria=(
            "使用用户明确选择的指标完成分析"
            if chinese
            else "Complete the analysis using the metric explicitly selected by the user",
        ),
    )
    return PlannerDecision(
        plan=plan,
        decision="clarify",
        clarification=AgentInputRequest(
            interrupt_id=f"metric-{digest}",
            reason=AgentInputReason.CLARIFICATION,
            prompt=prompt,
            choices=choices,
            allow_free_text=True,
        ),
        rationale_summary=(
            "用户未指定汇总指标，需要先澄清。"
            if chinese
            else "The requested aggregation metric is unspecified and requires clarification."
        ),
    )


__all__ = ["AnalysisPlanner"]
