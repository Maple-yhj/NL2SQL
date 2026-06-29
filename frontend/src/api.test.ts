import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "./api";
import type { Conversation, StoredSession } from "./types";

describe("ApiClient", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("patches conversation title and archived state", async () => {
    const updatedConversation = conversation("conv 1", "Renamed", true);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => updatedConversation,
    } as Response);
    const api = new ApiClient({
      getSession: () => session(),
      setSession: vi.fn(),
      clearSession: vi.fn(),
    });

    const result = await api.updateConversation("conv 1", {
      title: "Renamed",
      archived: true,
    });

    expect(result).toEqual(updatedConversation);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/conversations/conv%201");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({
      title: "Renamed",
      archived: true,
    });
    expect((init.headers as Headers).get("Authorization")).toBe("Bearer access-token");
  });
});

function conversation(
  conversationId: string,
  title: string,
  archived = false,
): Conversation {
  return {
    tenant_id: "demo",
    conversation_id: conversationId,
    user_id: "user-1",
    title,
    archived,
    created_at: "2026-06-29T00:00:00Z",
    updated_at: "2026-06-29T00:00:00Z",
  };
}

function session(): StoredSession {
  return {
    access_token: "access-token",
    refresh_token: "refresh-token",
    expires_in: 3600,
    token_type: "bearer",
    user: {
      tenant_id: "demo",
      user_id: "user-1",
      username: "yehj",
      roles: ["user"],
    },
  };
}
