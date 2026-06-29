import type { ChatMessage, DataRow, JsonValue, MessageType, TraceEntry } from "./types";

export interface AssistantViewModel {
  answer: string;
  rows: DataRow[];
  trace: TraceEntry[];
  intentLabel: string;
  status: "validated" | "error" | "pending";
  showSqlCard: false;
  showTable: boolean;
  isThinking: boolean;
  messageType: MessageType;
}

export function createAssistantViewModel(message: ChatMessage): AssistantViewModel {
  const metadata = message.metadata ?? {};
  const rows = Array.isArray(metadata.rows) ? metadata.rows : [];
  const trace = Array.isArray(metadata.trace) ? metadata.trace : [];
  const messageType = resolveMessageType(metadata.message_type, metadata.error, rows);
  const isThinking = messageType === "thinking";
  const answer = isThinking ? "" : metadata.answer || message.content || metadata.error || "";

  return {
    answer,
    rows,
    trace,
    intentLabel: formatIntent(metadata.intent),
    status: resolveStatus(metadata.ok, metadata.error),
    showSqlCard: false,
    showTable: messageType === "table" && rows.length > 0,
    isThinking,
    messageType,
  };
}

export function formatCellValue(value: JsonValue): string {
  if (value === null) {
    return "";
  }
  return formatCellValueForColumn(value, "");
}

export function formatCellValueForColumn(value: JsonValue, column: string): string {
  if (value === null) {
    return "";
  }
  if (isDateColumn(column) && typeof value === "string") {
    return formatDateTime(value);
  }
  if (isMoneyColumn(column) && typeof value === "number") {
    return value.toFixed(2);
  }
  if (Array.isArray(value) || typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

export function formatColumnLabel(column: string): string {
  return COLUMN_LABELS[column] ?? titleizeColumn(column);
}

export function getColumnClassName(column: string): string {
  return isNumericColumn(column) ? "numeric-cell" : "";
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

function resolveMessageType(
  messageType?: MessageType,
  error?: string,
  rows: DataRow[] = [],
): MessageType {
  if (messageType) {
    return messageType;
  }
  if (error) {
    return "error";
  }
  if (rows.some(isLikelyDetailRow) || rows.length > 1) {
    return "table";
  }
  return "text";
}

function isLikelyDetailRow(row: DataRow): boolean {
  const columns = Object.keys(row);
  if (columns.some((column) => column === "id" || column.endsWith("_id"))) {
    return true;
  }
  return columns.length >= 4;
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

const COLUMN_LABELS: Record<string, string> = {
  id: "订单ID",
  order_id: "订单ID",
  region: "地区",
  amount: "订单金额",
  gmv: "GMV",
  created_at: "创建时间",
  updated_at: "更新时间",
  quantity: "数量",
  product_id: "产品ID",
  user_id: "用户ID",
  customer_id: "客户ID",
  status: "状态",
};

const MONEY_COLUMNS = new Set(["amount", "gmv", "price", "total_amount", "order_amount"]);
const NUMERIC_COLUMNS = new Set([
  ...MONEY_COLUMNS,
  "quantity",
  "count",
  "cnt",
  "product_id",
  "user_id",
  "customer_id",
]);

function isDateColumn(column: string): boolean {
  return column.endsWith("_at") || column.endsWith("_time") || column === "date";
}

function isMoneyColumn(column: string): boolean {
  return MONEY_COLUMNS.has(column);
}

function isNumericColumn(column: string): boolean {
  return NUMERIC_COLUMNS.has(column);
}

function formatDateTime(value: string): string {
  const match = value.match(/^(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : value;
}

function titleizeColumn(column: string): string {
  return column
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
