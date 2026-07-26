export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type DataRow = Record<string, JsonValue>;
export type LogicalQueryPlan = Record<string, JsonValue>;
export type AgentMode = "plan" | "preview" | "execute";
export type MessageType = string;

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
  user_id: string;
  domain_id: string;
  conversation_id: string;
  title: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface ConversationUpdatePayload {
  title?: string;
  archived?: boolean;
}

export type ErrorCode =
    | "INVALID_REQUEST"
    | "CONFIG_INVALID"
    | "BUNDLE_NOT_FOUND"
    | "LOGICAL_PLAN_INVALID"
    | "BINDING_STALE"
    | "SQL_COMPILE_ERROR"
    | "SQL_POLICY_VIOLATION"
    | "COST_EXCEEDED"
    | "EMPTY_RESULT"
    | "JOIN_EXPLOSION"
    | "ACCESS_DENIED"
    | "RESULT_SEMANTIC_MISMATCH"
    | "TOOL_BUDGET_EXCEEDED"
    | "CONTEXT_BUDGET_EXCEEDED"
    | "DEADLINE_EXCEEDED"
    | "CANCELLED"
    | "INTERNAL_ERROR";

export interface AgentError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface RuntimeAgentError extends AgentError {
  code: ErrorCode;
}

export interface TraceEntry {
  node: string;
  status: string;
  error_code: string | null;
}

export interface PendingMemoryUpdate {
  scope: "working" | "conversation" | "user" | "episodic" | "enterprise";
  source: string;
  status: "pending_approval";
}

export interface ComponentVersionPin {
  component: string;
  version: string;
}

export interface RuntimeVersionPins {
  bundle_digest: string;
  runtime_version: string;
  domain_pack_digest: string;
  enterprise_binding_digest: string;
  deployment_profile_digest: string;
  schema_fingerprint: string;
  skill_id: string;
  skill_version: string;
  graph_id: string;
  graph_version: string;
  graph_digest: string;
  tool_registry_version: string;
  tool_versions: ComponentVersionPin[];
  model_versions: ComponentVersionPin[];
}

export interface AgentResponse {
  ok: boolean;
  question: string;
  contextualized_question: string | null;
  conversation_id: string | null;
  tenant_id: string | null;
  logical_plan: LogicalQueryPlan | null;
  sql: string | null;
  message_type: MessageType;
  rows: DataRow[];
  chart: ChartSpec | null;
  answer: string | null;
  error: RuntimeAgentError | null;
  trace: TraceEntry[];
  pending_memory_updates: PendingMemoryUpdate[];
  version_pins: RuntimeVersionPins | null;
}

export interface ChartSpec {
  chart_type: "bar";
  title: string;
  x_field: string;
  y_field: string;
}

export interface MessageMetadata {
  contextualized_question?: string | null;
  logical_plan?: LogicalQueryPlan | null;
  sql?: string | null;
  message_type?: MessageType;
  rows?: DataRow[];
  chart?: ChartSpec | null;
  answer?: string | null;
  ok?: boolean;
  error?: AgentError | null;
  error_code?: string | null;
  row_count?: number | null;
  trace?: TraceEntry[];
  pending_memory_updates?: PendingMemoryUpdate[];
  version_pins?: RuntimeVersionPins | null;
}

export interface ApiConversationMessage {
  role: "user" | "assistant" | "system";
  content: string;
  metadata: MessageMetadata;
}

export interface ChatMessage extends ApiConversationMessage {
  id: string;
}

export type ConversationMessageResponse = AgentResponse;

export interface SendMessagePayload {
  question: string;
  enterprise_id: string;
  domain_id: string;
  source_id?: string;
  source_version?: number;
  binding_id?: string;
  binding_version?: number;
  mode: AgentMode;
  requested_output: string;
  include_trace: boolean;
}

export type DataSourceKind = "postgres" | "sqlite" | "xlsx" | "csv";
export type DataSourceStatus = "registered" | "ready" | "error" | "disabled";

export interface DataSource {
  source_id: string;
  name: string;
  kind: DataSourceKind;
  status: DataSourceStatus;
  active_snapshot_version: number;
  options: Record<string, string | number | boolean>;
  created_at: string;
  updated_at: string;
}

export interface CatalogColumn {
  name: string;
  data_type: string;
  nullable: boolean;
}

export interface CatalogRelation {
  relation: string;
  columns: CatalogColumn[];
  estimated_rows: number | null;
  freshness_at: string | null;
}

export interface DataSourceCatalog {
  source_id: string;
  version: number;
  fingerprint: string;
  catalog: {
    schema_fingerprint: string;
    relations: CatalogRelation[];
  };
}

export type SemanticBindingStatus = "draft" | "active" | "retired";

export interface SemanticFieldMapping {
  logical_ref: string;
  physical_relation: string;
  physical_column: string;
}

export interface SemanticBinding {
  binding_id: string;
  tenant_id: string;
  source_id: string;
  source_snapshot_version: number;
  domain_id: string;
  version: number;
  status: SemanticBindingStatus;
  mappings: SemanticFieldMapping[];
  created_at: string;
  updated_at: string;
}

export interface SemanticBindingCreatePayload {
  binding_id?: string;
  domain_id: string;
  mappings: SemanticFieldMapping[];
}
