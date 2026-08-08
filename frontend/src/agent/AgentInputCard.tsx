import { FormEvent, useEffect, useState } from "react";
import { MessageSquareText, Send } from "lucide-react";

import type { AgentInputRequest } from "../types";

export function canSubmitAgentInput(value: string, enabled: boolean): boolean {
  return enabled && value.trim().length > 0;
}

export function AgentInputCard({
  request,
  disabled,
  onSubmit,
  onCancel,
}: {
  request: AgentInputRequest;
  disabled: boolean;
  onSubmit: (message: string, selectedChoice?: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const [choice, setChoice] = useState("");
  useEffect(() => {
    setValue("");
    setChoice("");
  }, [request.interrupt_id]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const message = choice || value.trim();
    if (canSubmitAgentInput(message, !disabled)) onSubmit(message, choice || undefined);
  }

  return (
    <form className="agent-input-card" onSubmit={submit}>
      <div className="agent-input-heading">
        <MessageSquareText size={17} />
        <div><strong>需要你的确认</strong><p>{request.prompt}</p></div>
      </div>
      {request.choices.length > 0 && (
        <div className="agent-input-choices">
          {request.choices.map((item) => (
            <button
              key={item}
              type="button"
              className={choice === item ? "selected" : ""}
              disabled={disabled}
              onClick={() => { setChoice(item); setValue(item); }}
            >
              {item}
            </button>
          ))}
        </div>
      )}
      {request.allow_free_text && (
        <textarea
          value={value}
          disabled={disabled}
          onChange={(event) => { setValue(event.target.value); setChoice(""); }}
          placeholder="输入补充信息"
          aria-label="恢复运行所需输入"
        />
      )}
      <div className="agent-input-actions">
        <button type="button" className="secondary-action" disabled={disabled} onClick={onCancel}>取消运行</button>
        <button type="submit" className="primary-action" disabled={!canSubmitAgentInput(choice || value, !disabled)}>
          <Send size={14} />继续分析
        </button>
      </div>
    </form>
  );
}
