import type { AgentMode, AnySemanticBinding, SendMessagePayload } from "./types";

export function createSendMessagePayload(
  question: string,
  binding: AnySemanticBinding,
  mode: AgentMode = "execute",
): SendMessagePayload {
  const payload: SendMessagePayload = {
    question: question.trim(),
    enterprise_id: "user-dataset",
    domain_id: binding.domain_id,
    source_id: binding.source_id,
    source_version: binding.source_snapshot_version,
    binding_id: binding.binding_id,
    binding_version: binding.version,
    mode,
    requested_output: "answer",
    include_trace: false,
  };
  return payload;
}
