import type {
  ApiConversationMessage,
  AuthUser,
  Conversation,
  ConversationUpdatePayload,
  ConversationMessageResponse,
  LoginPayload,
  SendMessagePayload,
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

  private async request<T>(
    path: string,
    init: RequestInit = {},
    authenticated = true,
    retryOnUnauthorized = true,
    acceptedErrorResponse?: (body: unknown) => body is T,
  ): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    const session = this.options.getSession();
    if (authenticated && session?.access_token) {
      headers.set("Authorization", `Bearer ${session.access_token}`);
    }

    const response = await fetch(path, { ...init, headers });
    if (response.status === 401 && authenticated && retryOnUnauthorized) {
      await this.refreshSession();
      return this.request<T>(path, init, authenticated, false, acceptedErrorResponse);
    }

    if (!response.ok) {
      const body = await readJsonBody(response);
      if (acceptedErrorResponse?.(body)) {
        return body;
      }
      throw new ApiError(response.status, readErrorMessage(body, response.statusText));
    }

    return response.json() as Promise<T>;
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
