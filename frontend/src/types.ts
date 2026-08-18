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
    | "AGENT_DECISION_INVALID"
    | "AGENT_ACTION_NOT_ALLOWED"
    | "AGENT_BUDGET_EXCEEDED"
    | "AGENT_MAX_STEPS_EXCEEDED"
    | "AGENT_EVIDENCE_INSUFFICIENT"
    | "AGENT_RESPONSE_UNGROUNDED"
    | "AGENT_WAITING_FOR_INPUT"
    | "AGENT_INTERRUPT_STALE"
    | "AGENT_RESUME_CONFLICT"
    | "AGENT_ARTIFACT_NOT_FOUND"
    | "AGENT_ARTIFACT_INTEGRITY_ERROR"
    | "AGENT_CHECKPOINT_UNAVAILABLE"
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

export interface DatasetRuntimeVersionPins {
  kind: "dataset";
  runtime_version: string;
  graph_id: string;
  graph_version: string;
  graph_digest: string;
  tool_registry_version: string;
  model_versions: ComponentVersionPin[];
  source_id: string;
  source_version: number;
  binding_id: string;
  binding_version: number;
  schema_fingerprint: string;
  relationship_graph_digest: string | null;
}

export type RuntimeVersionPins = DatasetRuntimeVersionPins;

export type AnalysisStepStatus =
  | "pending"
  | "running"
  | "completed"
  | "blocked"
  | "skipped";

export interface AnalysisStep {
  step_id: string;
  objective: string;
  status: AnalysisStepStatus;
  depends_on: string[];
  expected_evidence: string[];
}

export interface AnalysisPlan {
  plan_id: string;
  revision: number;
  steps: AnalysisStep[];
  completion_criteria: string[];
}

export interface AnalysisStepSummary {
  step_id: string;
  objective: string;
  status: AnalysisStepStatus | "waiting_input" | "failed";
  tool_names: string[];
  evidence_ids: string[];
}

export type AgentArtifactKind =
  | "catalog"
  | "logical_plan"
  | "prepared_query"
  | "query_preview"
  | "query_result"
  | "profile"
  | "computation"
  | "chart"
  | "answer";

export interface AgentArtifactSummary {
  artifact_id: string;
  kind: AgentArtifactKind;
  digest: string;
  row_count: number | null;
  sensitivity: "metadata" | "derived" | "row_data";
  created_at: string;
}

export interface EvidenceSummary {
  evidence_id: string;
  claim_key: string;
  artifact_id: string;
  field_refs: string[];
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
  analysis_plan: AnalysisPlan | null;
  analysis_steps: AnalysisStepSummary[];
  artifacts: AgentArtifactSummary[];
  evidence: EvidenceSummary[];
  limitations: string[];
}

export type AgentEventType =
  | "run_started"
  | "progress"
  | "context_resolved"
  | "plan_updated"
  | "step_started"
  | "tool_started"
  | "tool_completed"
  | "observation_recorded"
  | "run_waiting"
  | "run_resumed"
  | "answer_synthesizing"
  | "run_completed"
  | "run_failed";

export interface AgentInputRequest {
  interrupt_id: string;
  reason: "clarification" | "approval" | "conflict_resolution";
  origin?: "planner" | "evaluation" | "dataset_query";
  prompt: string;
  choices: string[];
  allow_free_text: boolean;
  action_id: string | null;
}

interface AgentEventBase<T extends AgentEventType, D> {
  type: T;
  run_id: string;
  sequence: number;
  data: D;
}

type NonResponseEvent<T extends AgentEventType, D> = AgentEventBase<T, D> & {
  response: null;
};

export type AgentEvent =
  | NonResponseEvent<"run_started", { kind: "run_started"; mode: AgentMode; enterprise_id: string; domain_id: string }>
  | NonResponseEvent<"progress", { kind: "progress"; stage: "versions_pinned"; pins: RuntimeVersionPins }>
  | NonResponseEvent<"context_resolved", { kind: "context_resolved"; source_id: string; source_version: number; binding_id: string; binding_version: number; schema_fingerprint: string }>
  | NonResponseEvent<"plan_updated", { kind: "plan_updated"; plan: AnalysisPlan }>
  | NonResponseEvent<"step_started", { kind: "step_started"; step_id: string; objective: string }>
  | NonResponseEvent<"tool_started", { kind: "tool_started"; call_id: string; action_id: string; tool_name: string; display_name: string; safe_arguments_digest: string }>
  | NonResponseEvent<"tool_completed", { kind: "tool_completed"; call_id: string; action_id: string; tool_name: string; status: "succeeded" | "failed"; artifacts: AgentArtifactSummary[]; evidence: EvidenceSummary[]; error_code: ErrorCode | null }>
  | NonResponseEvent<"observation_recorded", { kind: "observation_recorded"; observation_id: string; action_id: string; summary: string; artifact_ids: string[]; evidence_ids: string[] }>
  | NonResponseEvent<"run_waiting", { kind: "run_waiting"; input_request: AgentInputRequest }>
  | NonResponseEvent<"run_resumed", { kind: "run_resumed"; interrupt_id: string }>
  | NonResponseEvent<"answer_synthesizing", { kind: "answer_synthesizing"; evidence_ids: string[] }>
  | (AgentEventBase<"run_completed", { kind: "run_completed" }> & { response: AgentResponse })
  | (AgentEventBase<"run_failed", { kind: "run_failed"; error_code: ErrorCode }> & { response: AgentResponse });

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
  analysis_plan?: AnalysisPlan | null;
  analysis_steps?: AnalysisStepSummary[];
  artifacts?: AgentArtifactSummary[];
  evidence?: EvidenceSummary[];
  limitations?: string[];
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

export type WaitingAgentEvent = Extract<AgentEvent, { type: "run_waiting" }>;

export interface AgentWaitingResult {
  kind: "waiting";
  run_id: string;
  event: WaitingAgentEvent;
}

export type AgentRunResult = AgentResponse | AgentWaitingResult;

export interface RunResumePayload {
  interrupt_id: string;
  message: string;
  selected_choice?: string;
}

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
export interface RelationshipValidationReport { report_digest: string; activation_allowed: boolean; findings: Array<{ code: string; severity: "warning" | "error"; edge_id: string | null; node_id: string | null; message: string }>; }
export interface RelationshipRoutePreview { root_node_id: string; included_node_ids: string[]; route_digest: string; route_rule_id: string | null; steps: Array<{ edge_id: string; existing_node_id: string; introduced_node_id: string; traversal: "forward" | "reverse" }>; }
export type SemanticRole =
  | "identifier"
  | "dimension"
  | "measure"
  | "time"
  | "status"
  | "attribute";

export interface SemanticFieldMetadata {
  display_name?: string | null;
  description?: string | null;
  semantic_role?: SemanticRole | null;
  entity?: string | null;
  grain?: string | null;
  unit?: string | null;
  lifecycle_stage?: string | null;
  synonyms?: string[];
}

export interface SemanticMetricDefinition {
  metric_ref: string;
  display_name: string;
  description: string;
  operation:
    | "count"
    | "count_distinct"
    | "sum"
    | "avg"
    | "min"
    | "max"
    | "median";
  field_ref?: string | null;
  unit?: string | null;
  grain?: string | null;
  synonyms?: string[];
}

export type MetricScalar = string | number | boolean | null;
export type MetricAstNode = {
  kind: string;
  [key: string]: unknown;
};

export interface SemanticMetricDefinitionV2 {
  schema_version: 2;
  metric_ref: string;
  display_name: string;
  description: string;
  synonyms: string[];
  formula: MetricAstNode;
  default_filter: MetricAstNode | null;
  default_time_ref: string | null;
  allowed_time_refs: string[];
  entity_key_refs: string[];
  grain: string | null;
  unit: string | null;
  currency: string | null;
  currency_ref: string | null;
  null_policy: "exclude" | "zero" | "error";
  scope: {
    status_ref: string | null;
    included_statuses: string[];
    excluded_statuses: string[];
    refund_treatment: "gross" | "exclude_refunded" | "net_of_refunds" | "not_available";
    refund_ref: string | null;
    includes_freight: boolean | null;
    includes_tax: boolean | null;
    notes: string | null;
  };
  limitations: string[];
  owner: string | null;
  provenance: Array<Record<string, unknown>>;
}

export interface MetricProposalCandidate {
  candidate_id: string;
  definition: SemanticMetricDefinitionV2;
  label: string;
  rationale: string;
  required_decisions: string[];
}

export interface MetricProposal {
  proposal_id: string;
  revision: number;
  tenant_id: string;
  source_id: string;
  source_snapshot_version: number;
  schema_fingerprint: string;
  domain_id: string;
  base_binding_id: string;
  base_binding_version: number;
  requested_term: string;
  status: "draft" | "needs_clarification" | "validated" | "pending_approval" | "approved" | "rejected" | "superseded" | "expired";
  risk_tier: "low" | "medium" | "high";
  domain_pack: { pack_id: string; version: string; digest: string; domain_id: string } | null;
  candidates: MetricProposalCandidate[];
  selected_candidate_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface MetricValidationReport {
  report_id: string;
  proposal_id: string;
  proposal_revision: number;
  issues: Array<{ severity: "info" | "warning" | "error"; code: string; message: string; field_refs: string[] }>;
  activation_allowed: boolean;
}

export interface ActiveMetricSetPointer {
  metric_set_id: string;
  metric_set_version: number;
  metric_set_digest: string;
  revision: number;
  source_id: string;
  domain_id: string;
  binding_id: string;
  binding_version: number;
}

export interface MetricActivationResponse {
  proposal: MetricProposal;
  metric_set: Record<string, unknown>;
  active_pointer: ActiveMetricSetPointer;
}

export interface SemanticGraphFieldMapping extends SemanticFieldMetadata { logical_ref: string; node_id: string; column_id: string; }
export interface SemanticGraphBinding { schema_version: 2; binding_id: string; tenant_id: string; source_id: string; source_snapshot_version: number; schema_fingerprint: string; domain_id: string; version: number; status: SemanticBindingStatus; mappings: SemanticGraphFieldMapping[]; metrics?: SemanticMetricDefinition[]; validation_report_digest: string; created_at: string; updated_at: string; }

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

export interface SemanticFieldMapping extends SemanticFieldMetadata {
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
  metrics?: SemanticMetricDefinition[];
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
  metrics?: SemanticMetricDefinition[];
  primary_relation?: string | null;
  relationships?: SemanticRelationship[];
}
