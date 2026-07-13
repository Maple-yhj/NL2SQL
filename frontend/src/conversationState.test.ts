import { describe, expect, it } from "vitest";
import {
  applyConversationUpdate,
  removeConversationFromList,
  resolveConversationDeletionState,
} from "./conversationState";
import type { ChatMessage, Conversation } from "./types";

describe("conversation state helpers", () => {
  it("updates a renamed conversation without changing list order", () => {
    const first = conversation("conv-1", "Old title");
    const second = conversation("conv-2", "Second title");

    const items = applyConversationUpdate([first, second], {
      ...first,
      title: "New title",
    });

    expect(items.map((item) => item.conversation_id)).toEqual(["conv-1", "conv-2"]);
    expect(items[0].title).toBe("New title");
  });

  it("removes a deleted conversation from the sidebar list", () => {
    const remaining = removeConversationFromList(
      [conversation("conv-1", "First"), conversation("conv-2", "Second")],
      "conv-1",
    );

    expect(remaining.map((item) => item.conversation_id)).toEqual(["conv-2"]);
  });

  it("enters a new conversation state after deleting the active conversation", () => {
    const currentMessages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "show latest orders",
        metadata: {},
      },
    ];

    const next = resolveConversationDeletionState({
      activeConversationId: "conv-1",
      deletedConversationId: "conv-1",
      messages: currentMessages,
    });

    expect(next.activeConversationId).toBe("");
    expect(next.messages).toEqual([]);
  });

  it("keeps the current thread after deleting a different conversation", () => {
    const currentMessages: ChatMessage[] = [
      {
        id: "assistant-1",
        role: "assistant",
        content: "GMV is stable.",
        metadata: {},
      },
    ];

    const next = resolveConversationDeletionState({
      activeConversationId: "conv-2",
      deletedConversationId: "conv-1",
      messages: currentMessages,
    });

    expect(next.activeConversationId).toBe("conv-2");
    expect(next.messages).toBe(currentMessages);
  });
});

function conversation(conversationId: string, title: string): Conversation {
  return {
    tenant_id: "demo",
    domain_id: "commerce",
    conversation_id: conversationId,
    user_id: "user-1",
    title,
    archived: false,
    created_at: "2026-06-29T00:00:00Z",
    updated_at: "2026-06-29T00:00:00Z",
  };
}
