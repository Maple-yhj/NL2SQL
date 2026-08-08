import type { ChatMessage, Conversation } from "./types";

export function applyConversationUpdate(
  conversations: Conversation[],
  updatedConversation: Conversation,
): Conversation[] {
  return conversations.map((conversation) =>
    conversation.conversation_id === updatedConversation.conversation_id
      ? updatedConversation
      : conversation,
  );
}

export function removeConversationFromList(
  conversations: Conversation[],
  conversationId: string,
): Conversation[] {
  return conversations.filter((conversation) => conversation.conversation_id !== conversationId);
}

export interface ConversationDeletionStateInput {
  activeConversationId: string;
  deletedConversationId: string;
  messages: ChatMessage[];
}

export interface ConversationDeletionState {
  activeConversationId: string;
  messages: ChatMessage[];
}

export function resolveConversationDeletionState({
  activeConversationId,
  deletedConversationId,
  messages,
}: ConversationDeletionStateInput): ConversationDeletionState {
  if (activeConversationId !== deletedConversationId) {
    return { activeConversationId, messages };
  }

  return { activeConversationId: "", messages: [] };
}
