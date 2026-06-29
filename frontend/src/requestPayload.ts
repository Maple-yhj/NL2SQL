import type { SendMessagePayload } from "./types";

const REQUEST_DEFAULTS = {
  execute: true,
  timeout_ms: 10_000,
  max_limit: 1_000,
  max_validation_attempts: 2,
  memory_history_limit: 8,
} satisfies Omit<SendMessagePayload, "question">;

export function createSendMessagePayload(question: string): SendMessagePayload {
  return {
    question: question.trim(),
    ...REQUEST_DEFAULTS,
  };
}
