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
    | "GRAPH_NO_PATH"
    | "GRAPH_AMBIGUOUS_PATH"
    | "GRAPH_UNSAFE_FANOUT"
    | "GRAPH_STALE_SNAPSHOT"
    | "GRAPH_REVISION_CONFLICT"
    | "GRAPH_VALIDATION_FAILED"
    | "RELATIONSHIP_RECOMMENDATION_FAILED"
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
  dataset_query_plan: LogicalQueryPlan | null;
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

export type AgentEventType =
  | "run_started"
  | "progress"
  | "run_completed"
  | "run_failed";

export interface AgentEvent {
  type: AgentEventType;
  run_id: string;
  sequence: number;
  data: Record<string, JsonValue>;
  response: AgentResponse | null;
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
  dataset_query_plan?: LogicalQueryPlan | null;
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
  relationship_discovery?: { graph_id: string; run_id: string; status: string } | null;
}

export interface CatalogColumn {
  column_id: string;
  name: string;
  data_type: string;
  nullable: boolean;
  ordinal: number;
}

export interface CatalogRelation {
  relation_id: string;
  relation: string;
  columns: CatalogColumn[];
  keys: Array<{ key_id: string; kind: "primary" | "unique"; column_ids: string[] }>;
  foreign_keys: Array<{ foreign_key_id: string; from_relation_id: string; from_column_ids: string[]; to_relation_id: string; to_column_ids: string[] }>;
  estimated_rows: number | null;
  freshness_at: string | null;
}

export interface RelationshipGraphNode { node_id: string; relation_id: string; role_name: string; logical_entity: string; enabled: boolean; }
export interface RelationshipCondition { from_column_id: string; operator: "eq"; to_column_id: string; }
export interface RelationshipEdge { edge_id: string; from_node_id: string; to_node_id: string; conditions: RelationshipCondition[]; cardinality: "one_to_one" | "one_to_many" | "many_to_one" | "many_to_many" | "unknown"; join_semantics: "inner" | "left"; preserve_node_id: string | null; route_priority: number; enabled: boolean; provenance: Record<string, unknown>; quality: Record<string, unknown> | null; }
export interface RelationshipGraphDraft { graph_id: string; tenant_id: string; source_id: string; source_snapshot_version: number; schema_fingerprint: string; revision: number; status: "discovering" | "draft" | "validating" | "ready" | "failed"; nodes: RelationshipGraphNode[]; edges: RelationshipEdge[]; components: Array<Record<string, unknown>>; route_rules: Array<Record<string, unknown>>; }
export interface RelationshipRecommendationRun { run_id: string; source_id: string; graph_id: string; status: "queued" | "running" | "succeeded" | "failed" | "retryable_failed"; error_message: string | null; }
export interface RelationshipValidationReport { report_digest: string; activation_allowed: boolean; findings: Array<{ code: string; severity: "warning" | "error"; edge_id: string | null; message: string }>; }
export interface RelationshipRoutePreview { root_node_id: string; included_node_ids: string[]; route_digest: string; route_rule_id: string | null; steps: Array<{ edge_id: string; existing_node_id: string; introduced_node_id: string; traversal: "forward" | "reverse" }>; }
export interface SemanticGraphFieldMapping { logical_ref: string; node_id: string; column_id: string; }
export interface SemanticGraphBinding { schema_version: 2; binding_id: string; source_id: string; source_snapshot_version: number; schema_fingerprint: string; domain_id: string; version: number; status: SemanticBindingStatus; validation_report_digest: string; }

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

export type SemanticJoinType = "inner" | "left";

export interface SemanticRelationship {
  relationship_id: string;
  left_relation: string;
  left_column: string;
  right_relation: string;
  right_column: string;
  join_type: SemanticJoinType;
}

export interface SemanticBinding {
  schema_version?: 1;
  binding_id: string;
  tenant_id: string;
  source_id: string;
  source_snapshot_version: number;
  domain_id: string;
  version: number;
  status: SemanticBindingStatus;
  mappings: SemanticFieldMapping[];
  primary_relation?: string | null;
  relationships?: SemanticRelationship[];
  created_at: string;
  updated_at: string;
}

export type AnySemanticBinding = SemanticBinding | SemanticGraphBinding;

export interface SemanticBindingCreatePayload {
  binding_id?: string;
  domain_id: string;
  mappings: SemanticFieldMapping[];
  primary_relation?: string | null;
  relationships?: SemanticRelationship[];
}
