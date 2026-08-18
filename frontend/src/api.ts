import type {
  ApiConversationMessage,
  AgentEvent,
  AgentRunResult,
  AgentResponse,
  AgentWaitingResult,
  AuthUser,
  Conversation,
  ConversationUpdatePayload,
  ConversationMessageResponse,
  DataSource,
  DataSourceCatalog,
  LoginPayload,
  SendMessagePayload,
  SemanticBinding,
  AnySemanticBinding,
  SemanticBindingCreatePayload,
  RelationshipGraphDraft,
  RelationshipRecommendationRun,
  RelationshipRoutePreview,
  RelationshipValidationReport,
  RunResumePayload,
  SemanticGraphFieldMapping,
  SemanticGraphBinding,
  SemanticMetricDefinition,
  StoredSession,
  ActiveMetricSetPointer,
  MetricActivationResponse,
  MetricProposal,
  MetricProposalCandidate,
  MetricValidationReport,
} from "./types";
import { isAgentResponse } from "./agentResponseValidator";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface ApiClientOptions {
  getSession: () => StoredSession | null;
  setSession: (session: StoredSession) => void;
  clearSession: () => void;
}

export class ApiClient {
  constructor(private readonly options: ApiClientOptions) {}

  login(payload: LoginPayload): Promise<StoredSession> {
    return this.request<StoredSession>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    }, false);
  }

  async logout(refreshToken: string): Promise<void> {
    await this.request<{ ok: boolean }>("/api/auth/logout", {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshToken }),
    }, false);
  }

  me(): Promise<AuthUser> {
    return this.request<AuthUser>("/api/auth/me");
  }

  health(): Promise<{ ok: boolean; service: string }> {
    return this.request<{ ok: boolean; service: string }>("/health", {}, false);
  }

  listConversations(): Promise<{ items: Conversation[] }> {
    return this.request<{ items: Conversation[] }>(
      "/api/conversations?limit=20&include_archived=false",
    );
  }

  createConversation(title: string): Promise<Conversation> {
    return this.request<Conversation>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
  }

  updateConversation(
    conversationId: string,
    payload: ConversationUpdatePayload,
  ): Promise<Conversation> {
    return this.request<Conversation>(
      `/api/conversations/${encodeURIComponent(conversationId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    );
  }

  listMessages(conversationId: string): Promise<{ items: ApiConversationMessage[] }> {
    return this.request<{ items: ApiConversationMessage[] }>(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages?limit=50`,
    );
  }

  getConversationDataSourceBinding(
    conversationId: string,
  ): Promise<{ binding: AnySemanticBinding | null }> {
    return this.request<{ binding: AnySemanticBinding | null }>(
      `/api/conversations/${encodeURIComponent(conversationId)}/data-source-binding`,
    );
  }

  sendMessage(
    conversationId: string,
    payload: SendMessagePayload,
  ): Promise<ConversationMessageResponse> {
    return this.request<ConversationMessageResponse>(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
      true,
      true,
      isAgentResponse,
    );
  }

  async streamMessage(
    conversationId: string,
    payload: SendMessagePayload,
    onEvent: (event: AgentEvent) => void,
  ): Promise<AgentRunResult> {
    const response = await this.fetchResponse(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages/stream`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
    if (!response.ok) {
      const body = await readJsonBody(response);
      throw new ApiError(
        response.status,
        readErrorMessage(body, response.statusText),
      );
    }
    if (
      !response.headers.get("content-type")?.toLowerCase().includes(
        "text/event-stream",
      ) ||
      response.body === null
    ) {
      throw new ApiError(502, "Runtime did not return an event stream");
    }
    return readAgentEventStream(response.body, onEvent);
  }

  resumeRun(runId: string, payload: RunResumePayload): Promise<AgentRunResult> {
    return this.request<AgentRunResult>(
      `/api/runs/${encodeURIComponent(runId)}/resume`,
      { method: "POST", body: JSON.stringify(payload) },
    );
  }

  async streamResumeRun(
    runId: string,
    payload: RunResumePayload,
    onEvent: (event: AgentEvent) => void,
  ): Promise<AgentRunResult> {
    const response = await this.fetchResponse(
      `/api/runs/${encodeURIComponent(runId)}/resume/stream`,
      { method: "POST", body: JSON.stringify(payload) },
    );
    if (!response.ok) {
      const body = await readJsonBody(response);
      throw new ApiError(
        response.status,
        readErrorMessage(body, response.statusText),
      );
    }
    if (
      !response.headers.get("content-type")?.toLowerCase().includes(
        "text/event-stream",
      ) ||
      response.body === null
    ) {
      throw new ApiError(502, "Runtime did not return an event stream");
    }
    return readAgentEventStream(response.body, onEvent);
  }

  cancelRun(runId: string): Promise<{ run_id: string; cancelled: boolean }> {
    return this.request<{ run_id: string; cancelled: boolean }>(
      `/api/runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" },
    );
  }

  listRunEvents(
    runId: string,
    afterSequence = -1,
  ): Promise<{ items: AgentEvent[] }> {
    return this.request<{ items: AgentEvent[] }>(
      `/api/runs/${encodeURIComponent(runId)}/events?after_sequence=${afterSequence}`,
    );
  }

  listDataSources(): Promise<{ items: DataSource[] }> {
    return this.request<{ items: DataSource[] }>("/api/data-sources");
  }

  getDataSourceCatalog(sourceId: string): Promise<DataSourceCatalog> {
    return this.request<DataSourceCatalog>(
      `/api/data-sources/${encodeURIComponent(sourceId)}/catalog`,
    );
  }

  getRelationshipGraphDraft(sourceId: string): Promise<RelationshipGraphDraft> {
    return this.request<RelationshipGraphDraft>(`/api/data-sources/${encodeURIComponent(sourceId)}/relationship-graphs/draft`);
  }

  rerunRelationshipRecommendations(sourceId: string): Promise<RelationshipRecommendationRun> {
    return this.request<RelationshipRecommendationRun>(`/api/data-sources/${encodeURIComponent(sourceId)}/relationship-recommendations`, { method: "POST" });
  }

  getRelationshipRecommendationRun(sourceId: string, runId: string): Promise<RelationshipRecommendationRun> {
    return this.request<RelationshipRecommendationRun>(`/api/data-sources/${encodeURIComponent(sourceId)}/relationship-recommendations/${encodeURIComponent(runId)}`);
  }

  saveRelationshipGraph(sourceId: string, graph: RelationshipGraphDraft, expectedRevision: number): Promise<RelationshipGraphDraft> {
    return this.request<RelationshipGraphDraft>(`/api/data-sources/${encodeURIComponent(sourceId)}/relationship-graphs/${encodeURIComponent(graph.graph_id)}?expected_revision=${expectedRevision}`, { method: "PATCH", body: JSON.stringify(graph) });
  }

  validateRelationshipGraph(sourceId: string, graphId: string): Promise<RelationshipValidationReport> {
    return this.request<RelationshipValidationReport>(`/api/data-sources/${encodeURIComponent(sourceId)}/relationship-graphs/${encodeURIComponent(graphId)}/validate`, { method: "POST" });
  }

  previewRelationshipRoute(sourceId: string, graphId: string, requiredNodeIds: string[]): Promise<RelationshipRoutePreview> {
    return this.request<RelationshipRoutePreview>(`/api/data-sources/${encodeURIComponent(sourceId)}/relationship-graphs/${encodeURIComponent(graphId)}/preview-route`, { method: "POST", body: JSON.stringify({ required_node_ids: requiredNodeIds }) });
  }

  activateRelationshipGraph(sourceId: string, graphId: string, payload: { domain_id: string; mappings: SemanticGraphFieldMapping[]; metrics?: SemanticMetricDefinition[]; binding_id?: string }): Promise<SemanticGraphBinding> {
    return this.request<SemanticGraphBinding>(`/api/data-sources/${encodeURIComponent(sourceId)}/relationship-graphs/${encodeURIComponent(graphId)}/activate`, { method: "POST", body: JSON.stringify(payload) });
  }

  uploadFileDataSource(
    name: string,
    files: File[],
    sourceId = "",
  ): Promise<DataSource> {
    const body = new FormData();
    body.set("name", name);
    if (sourceId.trim()) {
      body.set("source_id", sourceId.trim());
    }
    for (const file of files) {
      body.append("files", file);
    }
    return this.request<DataSource>("/api/data-sources/files", {
      method: "POST",
      body,
    });
  }

  uploadSqliteDataSource(
    name: string,
    file: File,
    sourceId = "",
  ): Promise<DataSource> {
    const body = new FormData();
    body.set("name", name);
    if (sourceId.trim()) {
      body.set("source_id", sourceId.trim());
    }
    body.set("file", file);
    return this.request<DataSource>("/api/data-sources/sqlite", {
      method: "POST",
      body,
    });
  }

  deleteDataSource(
    sourceId: string,
  ): Promise<{ source_id: string; deleted: boolean }> {
    return this.request<{ source_id: string; deleted: boolean }>(
      `/api/data-sources/${encodeURIComponent(sourceId)}`,
      { method: "DELETE" },
    );
  }

  listDataSourceBindings(
    sourceId: string,
  ): Promise<{ items: AnySemanticBinding[] }> {
    return this.request<{ items: AnySemanticBinding[] }>(
      `/api/data-sources/${encodeURIComponent(sourceId)}/bindings`,
    );
  }

  createDataSourceBinding(
    sourceId: string,
    payload: SemanticBindingCreatePayload,
  ): Promise<SemanticBinding> {
    return this.request<SemanticBinding>(
      `/api/data-sources/${encodeURIComponent(sourceId)}/bindings`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  }

  activateDataSourceBinding(
    sourceId: string,
    bindingId: string,
  ): Promise<SemanticBinding> {
    return this.request<SemanticBinding>(
      `/api/data-sources/${encodeURIComponent(sourceId)}/bindings/${encodeURIComponent(bindingId)}/activate`,
      { method: "POST" },
    );
  }

  listMetricProposals(sourceId: string): Promise<{ items: MetricProposal[] }> {
    return this.request<{ items: MetricProposal[] }>(
      `/api/data-sources/${encodeURIComponent(sourceId)}/metric-proposals`,
    );
  }

  getSemanticMetricFeatures(): Promise<{ domain_pack_discovery: boolean; web_discovery: boolean; provisional_overlays: boolean; auto_publish_alias: boolean }> {
    return this.request<{ domain_pack_discovery: boolean; web_discovery: boolean; provisional_overlays: boolean; auto_publish_alias: boolean }>(
      "/api/semantic-metrics/features",
    );
  }

  discoverMetricProposal(sourceId: string, domainId: string, requestedTerm: string): Promise<MetricProposal> {
    return this.request<MetricProposal>(
      `/api/data-sources/${encodeURIComponent(sourceId)}/metric-proposals/discover`,
      { method: "POST", body: JSON.stringify({ domain_id: domainId, requested_term: requestedTerm }) },
    );
  }

  selectMetricCandidate(proposalId: string, candidateId: string, expectedRevision: number): Promise<MetricProposal> {
    return this.request<MetricProposal>(
      `/api/metric-proposals/${encodeURIComponent(proposalId)}/select`,
      { method: "POST", body: JSON.stringify({ candidate_id: candidateId, expected_revision: expectedRevision }) },
    );
  }

  reviseMetricCandidate(proposalId: string, candidate: MetricProposalCandidate, expectedRevision: number): Promise<MetricProposal> {
    return this.request<MetricProposal>(
      `/api/metric-proposals/${encodeURIComponent(proposalId)}/candidates/${encodeURIComponent(candidate.candidate_id)}`,
      { method: "PATCH", body: JSON.stringify({ candidate, expected_revision: expectedRevision }) },
    );
  }

  validateMetricProposal(proposalId: string, expectedRevision: number): Promise<{ proposal: MetricProposal; report: MetricValidationReport }> {
    return this.request<{ proposal: MetricProposal; report: MetricValidationReport }>(
      `/api/metric-proposals/${encodeURIComponent(proposalId)}/validate`,
      { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) },
    );
  }

  createMetricOverlay(proposalId: string, validationReportId: string, conversationId: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      `/api/metric-proposals/${encodeURIComponent(proposalId)}/overlays`,
      { method: "POST", body: JSON.stringify({ validation_report_id: validationReportId, scope: "conversation", conversation_id: conversationId }) },
    );
  }

  approveAndActivateMetric(proposalId: string, validationReportId: string, expectedRevision: number, expectedPointerRevision: number): Promise<MetricActivationResponse> {
    return this.request<MetricActivationResponse>(
      `/api/metric-proposals/${encodeURIComponent(proposalId)}/approve-and-activate`,
      { method: "POST", body: JSON.stringify({ validation_report_id: validationReportId, expected_revision: expectedRevision, expected_pointer_revision: expectedPointerRevision }) },
    );
  }

  getActiveMetricSet(sourceId: string, domainId: string): Promise<{ active_pointer: ActiveMetricSetPointer | null }> {
    return this.request<{ active_pointer: ActiveMetricSetPointer | null }>(
      `/api/data-sources/${encodeURIComponent(sourceId)}/metric-sets/active?domain_id=${encodeURIComponent(domainId)}`,
    );
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    authenticated = true,
    retryOnUnauthorized = true,
    acceptedErrorResponse?: (body: unknown) => body is T,
  ): Promise<T> {
    const response = await this.fetchResponse(
      path,
      init,
      authenticated,
      retryOnUnauthorized,
    );

    if (!response.ok) {
      const body = await readJsonBody(response);
      if (acceptedErrorResponse?.(body)) {
        return body;
      }
      throw new ApiError(response.status, readErrorMessage(body, response.statusText));
    }

    return response.json() as Promise<T>;
  }

  private async fetchResponse(
    path: string,
    init: RequestInit = {},
    authenticated = true,
    retryOnUnauthorized = true,
  ): Promise<Response> {
    const headers = new Headers(init.headers);
    if (
      init.body &&
      !(init.body instanceof FormData) &&
      !headers.has("Content-Type")
    ) {
      headers.set("Content-Type", "application/json");
    }

    const session = this.options.getSession();
    if (authenticated && session?.access_token) {
      headers.set("Authorization", `Bearer ${session.access_token}`);
    }

    const response = await fetch(path, { ...init, headers });
    if (response.status === 401 && authenticated && retryOnUnauthorized) {
      await this.refreshSession();
      return this.fetchResponse(path, init, authenticated, false);
    }
    return response;
  }

  private async refreshSession(): Promise<void> {
    const session = this.options.getSession();
    if (!session?.refresh_token) {
      this.options.clearSession();
      throw new ApiError(401, "Authentication expired");
    }

    const refreshed = await this.request<StoredSession>("/api/auth/refresh", {
      method: "POST",
      body: JSON.stringify({ refresh_token: session.refresh_token }),
    }, false);

    this.options.setSession(refreshed);
  }
}

async function readAgentEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: AgentEvent) => void,
): Promise<AgentRunResult> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal: AgentResponse | null = null;
  let waiting: AgentWaitingResult | null = null;

  function consumeFrame(frame: string): void {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) {
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      throw new ApiError(502, "Runtime emitted malformed event JSON");
    }
    if (!isAgentEvent(parsed)) {
      throw new ApiError(502, "Runtime emitted an invalid event");
    }
    onEvent(parsed);
    if (parsed.type === "run_waiting") {
      waiting = { kind: "waiting", run_id: parsed.run_id, event: parsed };
    }
    if (parsed.response !== null) {
      if (terminal !== null) {
        throw new ApiError(502, "Runtime emitted multiple terminal responses");
      }
      terminal = parsed.response;
    }
  }

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      consumeFrame(frame);
    }
    if (done) {
      if (buffer.trim()) {
        consumeFrame(buffer);
      }
      break;
    }
  }
  if (terminal !== null) return terminal;
  if (waiting !== null) return waiting;
  throw new ApiError(502, "Runtime stream ended without a closing event");
}

export function isAgentEvent(value: unknown): value is AgentEvent {
  if (!isExactRecord(value, ["type", "run_id", "sequence", "data", "response"])) {
    return false;
  }
  const types = new Set([
    "run_started",
    "progress",
    "context_resolved",
    "plan_updated",
    "step_started",
    "tool_started",
    "tool_completed",
    "observation_recorded",
    "run_waiting",
    "run_resumed",
    "answer_synthesizing",
    "run_completed",
    "run_failed",
  ]);
  const response = value.response;
  if (
    typeof value.type !== "string" ||
    !types.has(value.type) ||
    !isNonBlankString(value.run_id) ||
    !Number.isInteger(value.sequence) ||
    Number(value.sequence) < 0 ||
    !isRecord(value.data) ||
    value.data.kind !== value.type
  ) {
    return false;
  }
  const terminal = value.type === "run_completed" || value.type === "run_failed";
  if (terminal !== (response !== null) || (response !== null && !isAgentResponse(response))) {
    return false;
  }
  if (value.type === "run_completed" && (!isRecord(response) || response.ok !== true)) {
    return false;
  }
  if (value.type === "run_failed" && (!isRecord(response) || response.ok !== false)) {
    return false;
  }
  return validateEventPayload(value.type, value.data, response);
}

function validateEventPayload(
  type: string,
  data: Record<string, unknown>,
  response: unknown,
): boolean {
  switch (type) {
    case "run_started":
      return isExactRecord(data, ["kind", "mode", "enterprise_id", "domain_id"]) &&
        ["plan", "preview", "execute"].includes(String(data.mode)) &&
        isNonBlankString(data.enterprise_id) && isNonBlankString(data.domain_id);
    case "progress":
      return isExactRecord(data, ["kind", "stage", "pins"]) &&
        data.stage === "versions_pinned" && isRecord(data.pins);
    case "context_resolved":
      return isExactRecord(data, ["kind", "source_id", "source_version", "binding_id", "binding_version", "schema_fingerprint"]) &&
        isNonBlankString(data.source_id) && isPositiveInteger(data.source_version) &&
        isNonBlankString(data.binding_id) && isPositiveInteger(data.binding_version) &&
        isSchemaFingerprint(data.schema_fingerprint);
    case "plan_updated":
      return isExactRecord(data, ["kind", "plan"]) && validateAnalysisPlan(data.plan);
    case "step_started":
      return isExactRecord(data, ["kind", "step_id", "objective"]) &&
        isNonBlankString(data.step_id) && isNonBlankString(data.objective);
    case "tool_started":
      return isExactRecord(data, ["kind", "call_id", "action_id", "tool_name", "display_name", "safe_arguments_digest"]) &&
        isNonBlankString(data.call_id) && isNonBlankString(data.action_id) &&
        isNonBlankString(data.tool_name) && isNonBlankString(data.display_name) &&
        isDigest(data.safe_arguments_digest);
    case "tool_completed":
      return isExactRecord(data, ["kind", "call_id", "action_id", "tool_name", "status", "artifacts", "evidence", "error_code"]) &&
        isNonBlankString(data.call_id) && isNonBlankString(data.action_id) &&
        isNonBlankString(data.tool_name) && ["succeeded", "failed"].includes(String(data.status)) &&
        Array.isArray(data.artifacts) && data.artifacts.every(validateArtifactSummary) &&
        Array.isArray(data.evidence) && data.evidence.every(validateEvidenceSummary) &&
        ((data.status === "failed") === (typeof data.error_code === "string"));
    case "observation_recorded":
      return isExactRecord(data, ["kind", "observation_id", "action_id", "summary", "artifact_ids", "evidence_ids"]) &&
        isNonBlankString(data.observation_id) && isNonBlankString(data.action_id) &&
        isNonBlankString(data.summary) && isStringArray(data.artifact_ids) &&
        isStringArray(data.evidence_ids);
    case "run_waiting":
      return isExactRecord(data, ["kind", "input_request"]) &&
        validateAgentInputRequest(data.input_request);
    case "run_resumed":
      return isExactRecord(data, ["kind", "interrupt_id"]) &&
        isNonBlankString(data.interrupt_id);
    case "answer_synthesizing":
      return isExactRecord(data, ["kind", "evidence_ids"]) && isStringArray(data.evidence_ids);
    case "run_completed":
      return isExactRecord(data, ["kind"]);
    case "run_failed":
      return isExactRecord(data, ["kind", "error_code"]) &&
        typeof data.error_code === "string" && isRecord(response) &&
        isRecord(response.error) && response.error.code === data.error_code;
    default:
      return false;
  }
}

function validateAgentInputRequest(value: unknown): boolean {
  const baseKeys = ["interrupt_id", "reason", "prompt", "choices", "allow_free_text", "action_id"];
  const keys = isRecord(value) && Object.hasOwn(value, "origin")
    ? [...baseKeys, "origin"]
    : baseKeys;
  return isExactRecord(value, keys) &&
    isNonBlankString(value.interrupt_id) &&
    ["clarification", "approval", "conflict_resolution"].includes(String(value.reason)) &&
    (!Object.hasOwn(value, "origin") ||
      ["planner", "evaluation", "dataset_query"].includes(String(value.origin))) &&
    isNonBlankString(value.prompt) && isStringArray(value.choices) &&
    typeof value.allow_free_text === "boolean" &&
    (value.action_id === null || isNonBlankString(value.action_id));
}

function validateAnalysisPlan(value: unknown): boolean {
  return isExactRecord(value, ["plan_id", "revision", "steps", "completion_criteria"]) &&
    isNonBlankString(value.plan_id) && isPositiveInteger(value.revision) &&
    Array.isArray(value.steps) && value.steps.every((step) =>
      isExactRecord(step, ["step_id", "objective", "status", "depends_on", "expected_evidence"]) &&
      isNonBlankString(step.step_id) && isNonBlankString(step.objective) &&
      ["pending", "running", "completed", "blocked", "skipped"].includes(String(step.status)) &&
      isStringArray(step.depends_on) && isStringArray(step.expected_evidence)) &&
    isStringArray(value.completion_criteria);
}

function validateArtifactSummary(value: unknown): boolean {
  return isExactRecord(value, ["artifact_id", "kind", "digest", "row_count", "sensitivity", "created_at"]) &&
    isNonBlankString(value.artifact_id) && isNonBlankString(value.kind) && isDigest(value.digest) &&
    (value.row_count === null || (Number.isInteger(value.row_count) && Number(value.row_count) >= 0)) &&
    ["metadata", "derived", "row_data"].includes(String(value.sensitivity)) &&
    isNonBlankString(value.created_at);
}

function validateEvidenceSummary(value: unknown): boolean {
  return isExactRecord(value, ["evidence_id", "claim_key", "artifact_id", "field_refs"]) &&
    isNonBlankString(value.evidence_id) && isNonBlankString(value.claim_key) &&
    isNonBlankString(value.artifact_id) && isStringArray(value.field_refs);
}

function isExactRecord(value: unknown, keys: string[]): value is Record<string, unknown> {
  return isRecord(value) && Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key));
}

function isNonBlankString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isPositiveInteger(value: unknown): boolean {
  return Number.isInteger(value) && Number(value) >= 1;
}

function isDigest(value: unknown): boolean {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isSchemaFingerprint(value: unknown): boolean {
  return typeof value === "string" && /^(?:sha256:)?[0-9a-f]{64}$/.test(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isNonBlankString) &&
    new Set(value).size === value.length;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJsonBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

function readErrorMessage(body: unknown, statusText: string): string {
  if (isRecord(body)) {
    if (typeof body.detail === "string") {
      return body.detail;
    }
    if (isRecord(body.detail) && typeof body.detail.message === "string") {
      return body.detail.message;
    }
    if (Array.isArray(body.detail)) {
      const issues = body.detail
        .map((issue) => {
          if (!isRecord(issue) || typeof issue.msg !== "string") return "";
          const location = Array.isArray(issue.loc)
            ? issue.loc
                .filter((part) => typeof part === "string" || typeof part === "number")
                .filter((part) => part !== "body" && part !== "query")
                .join(".")
            : "";
          return location ? `${location}：${issue.msg}` : issue.msg;
        })
        .filter(Boolean);
      if (issues.length) {
        return `请求参数无效：${issues.join("；")}`;
      }
      return "请求参数无效，请检查所选节点后重试";
    }
    if (
      isRecord(body.error) &&
      typeof body.error.message === "string"
    ) {
      return body.error.message;
    }
  }
  return statusText || "Request failed";
}
