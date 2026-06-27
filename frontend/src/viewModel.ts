import type { ChatMessage, DataRow, JsonValue, TraceEntry } from "./types";

export interface AssistantViewModel {
  answer: string;
  rows: DataRow[];
  trace: TraceEntry[];
  intentLabel: string;
  status: "validated" | "error" | "pending";
  showSqlCard: false;
}

export function createAssistantViewModel(message: ChatMessage): AssistantViewModel {
  const metadata = message.metadata ?? {};
  const rows = Array.isArray(metadata.rows) ? metadata.rows : [];
  const trace = Array.isArray(metadata.trace) ? metadata.trace : [];
  const answer = metadata.answer || message.content || metadata.error || "";

  return {
    answer,
    rows,
    trace,
    intentLabel: formatIntent(metadata.intent),
    status: resolveStatus(metadata.ok, metadata.error),
    showSqlCard: false,
  };
}

export function formatCellValue(value: JsonValue): string {
  if (value === null) {
    return "";
  }
  if (Array.isArray(value) || typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function resolveStatus(ok?: boolean, error?: string): AssistantViewModel["status"] {
  if (error) {
    return "error";
  }
  if (ok === true) {
    return "validated";
  }
  return "pending";
}

function formatIntent(intent?: Record<string, JsonValue>): string {
  if (!intent) {
    return "intent: unknown";
  }

  const metrics = formatList(intent.metrics);
  const dimensions = formatList(intent.dimensions);
  const parts = [metrics, dimensions].filter(Boolean);
  return parts.length ? `intent: ${parts.join(", ")}` : "intent: parsed";
}

function formatList(value: JsonValue | undefined): string {
  if (!Array.isArray(value)) {
    return "";
  }
  return value.map((item) => String(item)).join(", ");
}
