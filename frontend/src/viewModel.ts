import type {
  AgentError,
  ChartSpec,
  ChatMessage,
  DataRow,
  JsonValue,
  LogicalQueryPlan,
  MessageType,
  PendingMemoryUpdate,
  TraceEntry,
} from "./types";

export interface AssistantViewModel {
  answer: string;
  rows: DataRow[];
  chart: ChartSpec | null;
  trace: TraceEntry[];
  logicalPlan: LogicalQueryPlan | null;
  sql: string;
  error: AgentError | null;
  pendingMemoryUpdates: PendingMemoryUpdate[];
  status: "validated" | "error" | "pending";
  showSqlCard: boolean;
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
  const showTable =
    (messageType === "table" || messageType === "chart") && rows.length > 0;
  const rawAnswer = metadata.answer || message.content || metadata.error?.message || "";
  const answer = isThinking ? "" : formatAnswer(rawAnswer, showTable, rows);

  return {
    answer,
    rows,
    chart: metadata.chart ?? null,
    trace,
    logicalPlan: metadata.logical_plan ?? null,
    sql: metadata.sql ?? "",
    error: metadata.error ?? null,
    pendingMemoryUpdates: metadata.pending_memory_updates ?? [],
    status: resolveStatus(metadata.ok, metadata.error),
    showSqlCard: Boolean(metadata.sql),
    showTable,
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
  if (isDurationColumn(column) && typeof value === "string") {
    const duration = formatDuration(value);
    if (duration) {
      return duration;
    }
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

function resolveStatus(ok?: boolean, error?: AgentError | null): AssistantViewModel["status"] {
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
  error?: AgentError | null,
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

function formatAnswer(answer: string, showTable: boolean, rows: DataRow[]): string {
  if (!showTable) {
    return answer;
  }
  return stripDanglingTableIntro(
    stripFalsePreviewClaims(stripRepeatedTableRowLines(stripMarkdownTables(answer), rows)),
  );
}

const FALSE_PREVIEW_CLAIM_PATTERNS = [
  /\b(?:only|just)\s+(?:the\s+)?(?:first\s+)?\d+\s+(?:rows?|records?)\b/i,
  /\b(?:run|execute)\s+(?:the\s+)?original\s+query\b/i,
  /\b(?:remaining|other)\s+\d+\s+(?:rows?|records?|items?)\s+(?:are\s+)?(?:not\s+)?(?:listed|shown|displayed)\b/i,
  /\b(?:subset|preview|sample)\b/i,
  /\b(?:not all|partial)\s+(?:rows?|records?|data)\b/i,
  /\b(?:full|complete)\s+(?:list|rows?|records?|data|results?)\b/i,
  /[\u53ea\u4ec5][\u663e\u5c55]\u793a.*?\u524d\s*\d+\s*[\u884c\u6761]/,
  /[\u53ea\u4ec5][\u663e\u5c55]\u793a.*?\d+\s*[\u884c\u6761]/,
  /\u5b9e\u9645\u5e94\u8fd4\u56de/,
  /\u5176\u4f59\s*\d+\s*(?:\u4e2a|\u884c|\u6761)?.*?\u672a\u5217\u51fa/,
  /\u672a\u5217\u51fa/,
  /\u5982\u9700\u5b8c\u6574/,
  /\u6267\u884c\u539f\u67e5\u8be2/,
  /\u83b7\u53d6\u5168\u90e8\s*\d+\s*[\u884c\u6761]/,
  /\u4ec5\u5217\u51fa/,
  /\u5176\u4f59.*?\u672a.*?\u5c55\u793a/,
  /\u672a\u5728\u7ed3\u679c\u4e2d\u5c55\u793a/,
];
const TABLE_ROW_BULLET_PATTERN = /^\s*(?:[-*\u2022]|\d+[\.)\u3001])\s*(.+?)\s*$/;
const TABLE_INTRO_PATTERN = /(?:\u5982\u4e0b|\u5982\u4e0b\u6240\u793a|as follows)\s*[:\uff1a]?$/i;
const MEANINGFUL_TEXT_PATTERN = /[A-Za-z0-9\u4e00-\u9fff]/;

function stripRepeatedTableRowLines(text: string, rows: DataRow[]): string {
  const tokens = tableValueTokens(rows);
  if (tokens.length === 0) {
    return text.trim();
  }

  const lines = text.split(/\r?\n/);
  const repeatedFlags = lines.map((line) => looksLikeRepeatedTableRow(line, tokens));
  if (repeatedFlags.filter(Boolean).length < 2) {
    return text.trim();
  }

  return lines
    .filter((line, index) => !repeatedFlags[index] && !isTableIntroLine(line))
    .join("\n")
    .trim();
}

function tableValueTokens(rows: DataRow[]): string[] {
  const tokens = new Set<string>();
  rows.forEach((row) => {
    Object.values(row).forEach((value) => {
      if (value === null || Array.isArray(value) || typeof value === "object") {
        return;
      }
      const text = String(value).trim().toLowerCase();
      if (text.length >= 2 && text.length <= 80) {
        tokens.add(text);
      }
    });
  });
  return [...tokens].sort((left, right) => right.length - left.length);
}

function looksLikeRepeatedTableRow(line: string, tokens: string[]): boolean {
  const match = line.match(TABLE_ROW_BULLET_PATTERN);
  if (!match) {
    return false;
  }
  const body = match[1].trim().toLowerCase();
  return tokens.some((token) => {
    if (body === token) {
      return true;
    }
    if (!body.startsWith(token)) {
      return false;
    }
    return [":", "\uff1a", "-", "\u2013", "\u2014"].includes(body[token.length] ?? "");
  });
}

function stripDanglingTableIntro(text: string): string {
  const result = text
    .split(/\r?\n/)
    .filter((line) => line.trim() && !isTableIntroLine(line))
    .join("\n")
    .trim();
  return MEANINGFUL_TEXT_PATTERN.test(result) ? result : "";
}

function isTableIntroLine(line: string): boolean {
  return TABLE_INTRO_PATTERN.test(line.trim());
}

function stripFalsePreviewClaims(text: string): string {
  const sentences = splitSentences(text);
  if (sentences.length === 0) {
    return text.trim();
  }

  return sentences
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence && !hasFalsePreviewClaim(sentence))
    .join(" ")
    .replace(/\s+([\u3002\uff01\uff1f!?\.])/g, "$1")
    .trim();
}

function hasFalsePreviewClaim(sentence: string): boolean {
  return FALSE_PREVIEW_CLAIM_PATTERNS.some((pattern) => pattern.test(sentence));
}

function splitSentences(text: string): string[] {
  const sentences: string[] = [];
  let sentenceStart = 0;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (!isSentenceEnding(char)) {
      continue;
    }
    if (char === "." && isDigit(text[index - 1]) && isDigit(text[index + 1])) {
      continue;
    }

    sentences.push(text.slice(sentenceStart, index + 1));
    sentenceStart = index + 1;
  }

  if (sentenceStart < text.length) {
    sentences.push(text.slice(sentenceStart));
  }
  return sentences;
}

function isSentenceEnding(char: string | undefined): boolean {
  return char === "." || char === "!" || char === "?" || char === "\u3002" || char === "\uff01" || char === "\uff1f";
}

function isDigit(char: string | undefined): boolean {
  return char !== undefined && char >= "0" && char <= "9";
}

function stripMarkdownTables(text: string): string {
  const lines = text.split(/\r?\n/);
  const kept: string[] = [];
  let index = 0;

  while (index < lines.length) {
    if (isMarkdownTableStart(lines, index)) {
      index += 2;
      while (index < lines.length && isMarkdownTableRow(lines[index])) {
        index += 1;
      }
      continue;
    }

    kept.push(lines[index]);
    index += 1;
  }

  return kept.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

function isMarkdownTableStart(lines: string[], index: number): boolean {
  const header = lines[index] ?? "";
  const separator = lines[index + 1] ?? "";
  return isMarkdownTableRow(header) && isMarkdownTableSeparator(separator);
}

function isMarkdownTableRow(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.startsWith("|") && trimmed.endsWith("|") && pipeCount(trimmed) >= 2;
}

function isMarkdownTableSeparator(line: string): boolean {
  const trimmed = line.trim();
  return isMarkdownTableRow(trimmed) && trimmed.includes("-") && /^[\s|:-]+$/.test(trimmed);
}

function pipeCount(value: string): number {
  return [...value].filter((char) => char === "|").length;
}

function isLikelyDetailRow(row: DataRow): boolean {
  const columns = Object.keys(row);
  if (columns.some((column) => column === "id" || column.endsWith("_id"))) {
    return true;
  }
  return columns.length >= 4;
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

function isDurationColumn(column: string): boolean {
  const normalized = column.toLowerCase();
  return (
    normalized.includes("duration") ||
    normalized.includes("interval") ||
    normalized.includes("elapsed") ||
    normalized.includes("handoff")
  );
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

function formatDuration(value: string): string {
  const match = value.match(
    /^P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$/i,
  );
  if (!match) {
    return "";
  }

  const days = Math.floor(Number(match[1] ?? 0));
  const hours = Math.floor(Number(match[2] ?? 0));
  const minutes = Math.floor(Number(match[3] ?? 0));
  const seconds = Number(match[4] ?? 0);
  const totalSeconds = days * 86400 + hours * 3600 + minutes * 60 + seconds;
  const totalMinutes = Math.floor(totalSeconds / 60);

  if (totalSeconds > 0 && totalMinutes === 0) {
    return "\u4e0d\u8db31\u5206\u949f";
  }
  if (totalMinutes === 0) {
    return "0\u5206\u949f";
  }

  const displayDays = Math.floor(totalMinutes / 1440);
  const displayHours = Math.floor((totalMinutes % 1440) / 60);
  const displayMinutes = totalMinutes % 60;
  const parts: string[] = [];
  if (displayDays > 0) {
    parts.push(`${displayDays}\u5929`);
  }
  if (displayHours > 0) {
    parts.push(`${displayHours}\u5c0f\u65f6`);
  }
  if (displayMinutes > 0) {
    parts.push(`${displayMinutes}\u5206\u949f`);
  }
  return parts.join("");
}

function titleizeColumn(column: string): string {
  return column
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
