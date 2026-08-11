import type {
  AgentArtifactSummary,
  AgentEvent,
  AgentInputRequest,
  AgentResponse,
  AnalysisPlan,
  AnalysisStepSummary,
  EvidenceSummary,
} from "../types";

export type AgentRunStatus =
  | "idle"
  | "running"
  | "waiting"
  | "resuming"
  | "completed"
  | "failed"
  | "cancelled";

export interface AgentToolCallView {
  callId: string;
  actionId: string;
  toolName: string;
  displayName: string;
  status: "running" | "succeeded" | "failed";
}

export interface AgentObservationView {
  observationId: string;
  actionId: string;
  summary: string;
  artifactIds: string[];
  evidenceIds: string[];
}

export interface AgentRunState {
  runId: string;
  status: AgentRunStatus;
  lastSequence: number;
  events: AgentEvent[];
  eventDigests: Record<number, string>;
  plan: AnalysisPlan | null;
  stepSummaries: AnalysisStepSummary[];
  toolCalls: AgentToolCallView[];
  observations: AgentObservationView[];
  artifacts: AgentArtifactSummary[];
  evidence: EvidenceSummary[];
  inputRequest: AgentInputRequest | null;
  response: AgentResponse | null;
}

export type AgentRunAction =
  | { type: "reset" }
  | { type: "hydrate"; events: AgentEvent[] }
  | { type: "event"; event: AgentEvent }
  | { type: "resume_requested" }
  | { type: "stream_failed" }
  | { type: "cancelled" };

export function createAgentRunState(): AgentRunState {
  return {
    runId: "",
    status: "idle",
    lastSequence: -1,
    events: [],
    eventDigests: {},
    plan: null,
    stepSummaries: [],
    toolCalls: [],
    observations: [],
    artifacts: [],
    evidence: [],
    inputRequest: null,
    response: null,
  };
}

export function agentRunReducer(
  state: AgentRunState,
  action: AgentRunAction,
): AgentRunState {
  if (action.type === "reset") return createAgentRunState();
  if (action.type === "hydrate") return hydrateAgentRun(action.events);
  if (action.type === "resume_requested") {
    if (state.status !== "waiting") return state;
    return { ...state, status: "resuming" };
  }
  if (action.type === "cancelled") {
    return terminateActiveWork({
      ...state,
      status: "cancelled",
      inputRequest: null,
    });
  }
  if (action.type === "stream_failed") {
    return terminateActiveWork({
      ...state,
      status: "failed",
      inputRequest: null,
    });
  }
  return applyEvent(state, action.event);
}

export function hydrateAgentRun(events: AgentEvent[]): AgentRunState {
  return [...events]
    .sort((left, right) => left.sequence - right.sequence)
    .reduce(
      (state, current) => agentRunReducer(state, { type: "event", event: current }),
      createAgentRunState(),
    );
}

function applyEvent(state: AgentRunState, event: AgentEvent): AgentRunState {
  const digest = JSON.stringify(event);
  const existingDigest = state.eventDigests[event.sequence];
  if (existingDigest !== undefined) {
    if (existingDigest === digest) return state;
    throw new Error("conflicting Agent event replay");
  }
  if (state.runId && event.run_id !== state.runId) {
    throw new Error("Agent event belongs to a different run");
  }
  if (event.sequence !== state.lastSequence + 1) {
    throw new Error("Agent event sequence is not contiguous");
  }

  let next: AgentRunState = {
    ...state,
    runId: event.run_id,
    status: state.status === "idle" ? "running" : state.status,
    lastSequence: event.sequence,
    events: [...state.events, event],
    eventDigests: { ...state.eventDigests, [event.sequence]: digest },
  };
  switch (event.type) {
    case "run_started":
    case "progress":
    case "context_resolved":
    case "answer_synthesizing":
      next.status = "running";
      break;
    case "plan_updated":
      next.plan = event.data.plan;
      next.status = "running";
      break;
    case "step_started":
      next.plan = next.plan
        ? {
            ...next.plan,
            steps: next.plan.steps.map((step) =>
              step.step_id === event.data.step_id
                ? { ...step, status: "running" as const }
                : step,
            ),
          }
        : null;
      break;
    case "tool_started":
      next.toolCalls = [
        ...next.toolCalls,
        {
          callId: event.data.call_id,
          actionId: event.data.action_id,
          toolName: event.data.tool_name,
          displayName: event.data.display_name,
          status: "running",
        },
      ];
      break;
    case "tool_completed":
      next.toolCalls = next.toolCalls.map((call) =>
        call.callId === event.data.call_id
          ? { ...call, status: event.data.status }
          : call,
      );
      next.artifacts = mergeBy(
        next.artifacts,
        event.data.artifacts,
        (item) => item.artifact_id,
      );
      next.evidence = mergeBy(
        next.evidence,
        event.data.evidence,
        (item) => item.evidence_id,
      );
      break;
    case "observation_recorded":
      next.observations = [
        ...next.observations,
        {
          observationId: event.data.observation_id,
          actionId: event.data.action_id,
          summary: event.data.summary,
          artifactIds: event.data.artifact_ids,
          evidenceIds: event.data.evidence_ids,
        },
      ];
      break;
    case "run_waiting":
      next.status = "waiting";
      next.inputRequest = event.data.input_request;
      break;
    case "run_resumed":
      next.status = "running";
      next.inputRequest = null;
      break;
    case "run_completed":
      next = applyResponse(next, event.response);
      next.status = "completed";
      next.inputRequest = null;
      break;
    case "run_failed":
      next = applyResponse(next, event.response);
      next.status =
        event.data.error_code === "CANCELLED" ? "cancelled" : "failed";
      next.inputRequest = null;
      next = terminateActiveWork(next);
      break;
  }
  return next;
}

function terminateActiveWork(state: AgentRunState): AgentRunState {
  return {
    ...state,
    plan: state.plan
      ? {
          ...state.plan,
          steps: state.plan.steps.map((step) =>
            step.status === "running"
              ? { ...step, status: "blocked" as const }
              : step,
          ),
        }
      : null,
    toolCalls: state.toolCalls.map((call) =>
      call.status === "running" ? { ...call, status: "failed" as const } : call,
    ),
  };
}

function applyResponse(state: AgentRunState, response: AgentResponse): AgentRunState {
  return {
    ...state,
    response,
    plan: response.analysis_plan ?? state.plan,
    stepSummaries: response.analysis_steps,
    artifacts: mergeBy(state.artifacts, response.artifacts, (item) => item.artifact_id),
    evidence: mergeBy(state.evidence, response.evidence, (item) => item.evidence_id),
  };
}

function mergeBy<T>(current: T[], additions: T[], key: (item: T) => string): T[] {
  const values = new Map(current.map((item) => [key(item), item]));
  for (const item of additions) values.set(key(item), item);
  return [...values.values()];
}
