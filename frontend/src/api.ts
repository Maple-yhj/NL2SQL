import type {
  ApiConversationMessage,
  AgentEvent,
  AgentResponse,
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
  SemanticGraphFieldMapping,
  SemanticGraphBinding,
  StoredSession,
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
  ): Promise<ConversationMessageResponse> {
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

  activateRelationshipGraph(sourceId: string, graphId: string, payload: { domain_id: string; mappings: SemanticGraphFieldMapping[]; binding_id?: string }): Promise<SemanticGraphBinding> {
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
): Promise<AgentResponse> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal: AgentResponse | null = null;

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
  if (terminal === null) {
    throw new ApiError(502, "Runtime stream ended without a terminal response");
  }
  return terminal;
}

function isAgentEvent(value: unknown): value is AgentEvent {
  if (!isRecord(value)) {
    return false;
  }
  const types = new Set([
    "run_started",
    "progress",
    "run_completed",
    "run_failed",
  ]);
  const response = value.response;
  return (
    typeof value.type === "string" &&
    types.has(value.type) &&
    typeof value.run_id === "string" &&
    value.run_id.trim().length > 0 &&
    Number.isInteger(value.sequence) &&
    Number(value.sequence) >= 0 &&
    isRecord(value.data) &&
    (response === null || isAgentResponse(response)) &&
    ((value.type === "run_completed" || value.type === "run_failed") ===
      (response !== null))
  );
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
    if (
      isRecord(body.error) &&
      typeof body.error.message === "string"
    ) {
      return body.error.message;
    }
  }
  return statusText || "Request failed";
}
