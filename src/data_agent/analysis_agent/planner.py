"""Provider-neutral structured planner for the native analysis Agent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from data_agent.tools.models import ToolSpec

from .models import (
    AgentContextSnapshot,
    AgentObservation,
    AnalysisGoal,
    AnalysisPlan,
    PlannerDecision,
)
from .prompts import (
    ModelClient,
    PLANNER_SYSTEM_PROMPT,
    bounded_text,
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
    ) -> PlannerDecision:
        specs = {spec.name: spec for spec in allowed_tools}
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
                decision,
                current_plan=current_plan,
                specs=specs,
            )

        return await complete_strict_model(
            model_client=self._model_client,
            prompt=prompt,
            system=PLANNER_SYSTEM_PROMPT,
            output_type=PlannerDecision,
            task="planner_decision",
            max_attempts=self._max_attempts,
            validator=validate,
        )

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
        spec = specs.get(decision.next_action.tool_name)
        if spec is None:
            raise ValueError("planner selected a tool outside the allowed registry view")
        try:
            spec.input_schema.model_validate(decision.next_action.arguments)
        except (TypeError, ValueError) as exc:
            raise ValueError("planner action does not match the allowed tool input schema") from exc

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


__all__ = ["AnalysisPlanner"]
