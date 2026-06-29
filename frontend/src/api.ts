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
    );
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    authenticated = true,
    retryOnUnauthorized = true,
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
      return this.request<T>(path, init, authenticated, false);
    }

    if (!response.ok) {
      throw new ApiError(response.status, await readErrorMessage(response));
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

async function readErrorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") {
      return body.detail;
    }
    if (typeof body.error === "string") {
      return body.detail ? `${body.error}: ${body.detail}` : body.error;
    }
  } catch {
    return response.statusText || "Request failed";
  }
  return response.statusText || "Request failed";
}
