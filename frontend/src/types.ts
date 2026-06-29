export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type DataRow = Record<string, JsonValue>;

export type MessageType = "text" | "table" | "error" | "thinking";

export interface AuthUser {
  tenant_id: string;
  user_id: string;
  username: string;
  roles: string[];
}

export interface StoredSession {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
  user: AuthUser;
}

export interface LoginPayload {
  tenant_id: string;
  username: string;
  password: string;
}

export interface Conversation {
  tenant_id: string;
  conversation_id: string;
  user_id: string;
  title: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface ConversationUpdatePayload {
  title?: string;
  archived?: boolean;
}

export interface TraceEntry {
  node?: string;
  ok?: boolean;
  message?: string;
  [key: string]: JsonValue | undefined;
}

export interface MessageMetadata {
  contextualized_question?: string;
  sql?: string;
  message_type?: MessageType;
  rows?: DataRow[];
  answer?: string;
  ok?: boolean;
  error?: string;
  trace?: TraceEntry[];
  intent?: Record<string, JsonValue>;
}

export interface ApiConversationMessage {
  role: "user" | "assistant";
  content: string;
  metadata: MessageMetadata;
}

export interface ChatMessage extends ApiConversationMessage {
  id: string;
}

export interface ConversationMessageResponse {
  ok: boolean;
  question: string;
  contextualized_question: string;
  conversation_id: string;
  user_id: string;
  tenant_id: string;
  intent: Record<string, JsonValue>;
  sql: string;
  message_type: Exclude<MessageType, "thinking">;
  rows: DataRow[];
  answer: string;
  error: string;
  trace: TraceEntry[];
}

export interface SendMessagePayload {
  question: string;
  execute: boolean;
  timeout_ms: number;
  max_limit: number;
  max_validation_attempts: number;
  memory_history_limit: number;
}
