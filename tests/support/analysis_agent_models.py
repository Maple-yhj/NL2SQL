"""Deterministic model doubles shared by analysis-agent integration tests."""

from __future__ import annotations

import json


class AgentTrajectoryModel:
    """Drive the compile -> execute -> evidence trajectory without network I/O."""

    model_id = "test.dataset-analysis-agent"
    version = "1"

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        del system, max_output_tokens
        document = json.loads(prompt)
        task = document["task"]
        if task in {"create_dataset_query_program", "repair_dataset_query_program"}:
            return json.dumps(
                {
                    "schema_version": 2,
                    "status": "ready",
                    "stages": [
                        {
                            "kind": "query",
                            "stage_id": "summary",
                            "input": {
                                "kind": "dataset",
                                "anchor_ref": "dataset.Orders.total",
                            },
                            "projections": [
                                {
                                    "alias": "total_amount",
                                    "expression": {
                                        "kind": "aggregate",
                                        "operation": "sum",
                                        "operand": {
                                            "kind": "field",
                                            "ref": "dataset.Orders.total",
                                        },
                                        "filter": None,
                                    },
                                }
                            ],
                            "filters": [],
                            "group_by": [],
                            "order_by": [],
                            "limit": None,
                        }
                    ],
                    "output_stage_id": "summary",
                    "clarification_question": None,
                }
            )
        untrusted = document["untrustedData"]
        if task == "plan_or_replan_analysis":
            observations = untrusted["safeObservations"]
            plan = untrusted["currentPlan"] or {
                "plan_id": "dual-run-plan",
                "revision": 1,
                "steps": [
                    {
                        "step_id": "compile",
                        "objective": "Compile the governed aggregate query",
                        "status": "pending",
                        "depends_on": [],
                        "expected_evidence": [],
                    },
                    {
                        "step_id": "execute",
                        "objective": "Execute the governed aggregate query",
                        "status": "pending",
                        "depends_on": ["compile"],
                        "expected_evidence": [],
                    },
                    {
                        "step_id": "evidence",
                        "objective": "Bind the aggregate result to evidence",
                        "status": "pending",
                        "depends_on": ["execute"],
                        "expected_evidence": ["total_amount"],
                    },
                ],
                "completion_criteria": ["total_amount is evidence-backed"],
            }
            if len(observations) == 0:
                tool_name = "query.compile"
                arguments = {
                    "plan": {
                        "analysis_type": "aggregate",
                        "aggregations": [
                            {
                                "ref": "dataset.Orders.total",
                                "operation": "sum",
                                "alias": "total_amount",
                            }
                        ],
                        "limit": 100,
                    }
                }
            elif len(observations) == 1:
                tool_name = "query.execute"
                arguments = {
                    "artifact_id": observations[0]["artifacts"][0]["artifactId"],
                    "preview_rows": 20,
                }
            else:
                tool_name = "evidence.collect"
                arguments = {
                    "artifact_id": observations[1]["artifacts"][0]["artifactId"],
                    "claim_key": "total_amount",
                    "field_refs": ["total_amount"],
                }
            return json.dumps(
                {
                    "plan": plan,
                    "decision": "act",
                    "next_action": {
                        "action_id": f"dual-action-{len(observations) + 1}",
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "purpose": "Produce the next governed artifact",
                        "expected_evidence": ["total_amount"],
                    },
                    "rationale_summary": "Advance the fixed governed trajectory.",
                }
            )
        if task == "evaluate_analysis_progress":
            observations = untrusted["safeObservations"]
            completed = [
                item["step_id"]
                for item in untrusted["plan"]["steps"]
                if item["status"] == "completed"
            ]
            finished = len(observations) == 3
            return json.dumps(
                {
                    "decision": "finish" if finished else "continue",
                    "evidence_sufficient": finished,
                    "completed_step_ids": completed,
                    "missing_evidence": [] if finished else ["total_amount"],
                    "contradictions": [],
                    "rationale_summary": (
                        "Evidence is complete." if finished else "Continue."
                    ),
                }
            )
        if task == "synthesize_grounded_analysis_answer":
            evidence_id = untrusted["validatedEvidence"][0]["evidenceId"]
            return json.dumps(
                {
                    "answer": "The total amount is 35.",
                    "key_findings": [
                        {
                            "finding_id": "total-amount",
                            "claim": "The total amount is 35.",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "recommended_chart_artifact_id": None,
                    "limitations": [],
                    "evidence_ids": [evidence_id],
                }
            )
        raise AssertionError(f"unexpected model task: {task}")
