import type { AgentMode, SendMessagePayload } from "./types";

export function createSendMessagePayload(
  question: string,
  mode: AgentMode = "execute",
): SendMessagePayload {
  return {
    question: question.trim(),
    enterprise_id: "olist",
    domain_id: "commerce",
    mode,
    requested_output: "answer",
    include_trace: false,
  };
}
