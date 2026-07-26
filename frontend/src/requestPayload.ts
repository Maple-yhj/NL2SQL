import type { AgentMode, SemanticBinding, SendMessagePayload } from "./types";

export function createSendMessagePayload(
  question: string,
  mode: AgentMode = "execute",
  binding: SemanticBinding | null = null,
): SendMessagePayload {
  const payload: SendMessagePayload = {
    question: question.trim(),
    enterprise_id: binding ? "user-dataset" : "olist",
    domain_id: binding?.domain_id ?? "commerce",
    mode,
    requested_output: "answer",
    include_trace: false,
  };
  if (binding) {
    payload.source_id = binding.source_id;
    payload.source_version = binding.source_snapshot_version;
    payload.binding_id = binding.binding_id;
    payload.binding_version = binding.version;
  }
  return payload;
}
