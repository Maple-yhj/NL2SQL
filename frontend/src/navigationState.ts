export function getNewChatButtonClassName(activeConversationId: string): string {
  return activeConversationId ? "sidebar-nav-item" : "sidebar-nav-item active";
}
