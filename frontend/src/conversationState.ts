import type { ChatMessage, Conversation } from "./types";

export function conversationTitleFromQuestion(question: string): string {
  const singleLine = question.replace(/\s+/g, " ").trim();
  return singleLine.length <= 54 ? singleLine : `${singleLine.slice(0, 53)}…`;
}

export function isDefaultConversationTitle(title: string | null | undefined): boolean {
  const value = (title ?? "").trim();
  return !value || value === "New analysis" || value === "Untitled analysis";
}

export function conversationMatchesSearch(
  conversation: Conversation,
  query: string,
  messages: ChatMessage[] = [],
): boolean {
  const normalized = query.trim().toLocaleLowerCase("zh-CN");
  if (!normalized) return true;
  return [
    conversation.title || "未命名分析",
    ...messages.map((message) => message.content),
  ].some((value) => value.toLocaleLowerCase("zh-CN").includes(normalized));
}

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
