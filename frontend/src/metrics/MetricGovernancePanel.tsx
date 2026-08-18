import { FormEvent, useEffect, useMemo, useState } from "react";
import { CheckCircle2, FlaskConical, Loader2, Search, ShieldCheck } from "lucide-react";

import type { ApiClient } from "../api";
import type {
  MetricAstNode,
  MetricProposal,
  MetricProposalCandidate,
  MetricValidationReport,
} from "../types";

function formulaText(node: MetricAstNode | unknown): string {
  if (!node || typeof node !== "object") return "—";
  const value = node as Record<string, unknown>;
  if (value.kind === "field") return String(value.ref);
  if (value.kind === "literal" || value.kind === "constant") return JSON.stringify(value.value);
  if (value.kind === "aggregate") {
    return `${String(value.operation).toUpperCase()}(${value.operand ? formulaText(value.operand) : "*"})`;
  }
  if (value.kind === "binary" || value.kind === "formula_binary") {
    const operator = { add: "+", subtract: "−", multiply: "×", divide: "÷" }[String(value.operation)] ?? String(value.operation);
    return `(${formulaText(value.left)} ${operator} ${formulaText(value.right)})`;
  }
  if (value.kind === "function") {
    return `${String(value.operation)}(${(value.arguments as unknown[]).map(formulaText).join(", ")})`;
  }
  return String(value.kind ?? "formula");
}

function replaceProposal(items: MetricProposal[], proposal: MetricProposal): MetricProposal[] {
  return [proposal, ...items.filter((item) => item.proposal_id !== proposal.proposal_id)];
}

export function MetricGovernancePanel({
  api,
  sourceId,
  domainId,
  conversationId,
}: {
  api: ApiClient;
  sourceId: string;
  domainId: string;
  conversationId?: string;
}) {
  const [term, setTerm] = useState("GMV");
  const [proposals, setProposals] = useState<MetricProposal[]>([]);
  const [reports, setReports] = useState<Record<string, MetricValidationReport>>({});
  const [overlayProposals, setOverlayProposals] = useState<Record<string, boolean>>({});
  const [overlaysEnabled, setOverlaysEnabled] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setError("");
    void api.listMetricProposals(sourceId)
      .then((payload) => { if (active) setProposals(payload.items); })
      .catch((reason: Error) => { if (active) setError(reason.message); });
    void api.getSemanticMetricFeatures()
      .then((features) => { if (active) setOverlaysEnabled(features.provisional_overlays); })
      .catch(() => { if (active) setOverlaysEnabled(false); });
    return () => { active = false; };
  }, [api, sourceId]);

  async function discover(event: FormEvent) {
    event.preventDefault();
    if (!term.trim()) return;
    setBusy("discover"); setError("");
    try {
      const proposal = await api.discoverMetricProposal(sourceId, domainId, term.trim());
      setProposals((items) => replaceProposal(items, proposal));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法生成指标候选");
    } finally { setBusy(""); }
  }

  async function validate(proposal: MetricProposal) {
    setBusy(`validate:${proposal.proposal_id}`); setError("");
    try {
      const result = await api.validateMetricProposal(proposal.proposal_id, proposal.revision);
      setProposals((items) => replaceProposal(items, result.proposal));
      setReports((items) => ({ ...items, [proposal.proposal_id]: result.report }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "指标校验失败");
    } finally { setBusy(""); }
  }

  async function approve(proposal: MetricProposal) {
    const report = reports[proposal.proposal_id];
    if (!report) return;
    setBusy(`approve:${proposal.proposal_id}`); setError("");
    try {
      const active = await api.getActiveMetricSet(sourceId, domainId);
      const result = await api.approveAndActivateMetric(
        proposal.proposal_id,
        report.report_id,
        proposal.revision,
        active.active_pointer?.revision ?? 0,
      );
      setProposals((items) => replaceProposal(items, result.proposal));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "指标激活失败");
    } finally { setBusy(""); }
  }

  async function useInConversation(proposal: MetricProposal) {
    const report = reports[proposal.proposal_id];
    if (!report || !conversationId) return;
    setBusy(`overlay:${proposal.proposal_id}`); setError("");
    try {
      await api.createMetricOverlay(proposal.proposal_id, report.report_id, conversationId);
      setOverlayProposals((items) => ({ ...items, [proposal.proposal_id]: true }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "临时口径绑定失败");
    } finally { setBusy(""); }
  }

  return (
    <section className="metric-governance-panel">
      <header>
        <div><ShieldCheck size={18} /><span><strong>领域指标治理</strong><small>{domainId}</small></span></div>
        <span className="status-stamp">Domain Pack → Binding</span>
      </header>
      <p>领域包只生成候选；公式与范围通过校验和审批后才会成为组织口径。</p>
      <form className="metric-discovery-form" onSubmit={discover}>
        <input value={term} onChange={(event) => setTerm(event.target.value)} aria-label="待识别指标" placeholder="例如 GMV、成交总额" />
        <button className="secondary-action" disabled={busy === "discover" || !term.trim()}>
          {busy === "discover" ? <Loader2 className="spin" size={14} /> : <Search size={14} />}生成候选
        </button>
      </form>
      {error && <div className="metric-governance-error">{error}</div>}
      <div className="metric-proposal-list">
        {proposals.map((proposal) => (
          <article className="metric-proposal" key={proposal.proposal_id}>
            <div className="metric-proposal-heading">
              <span><strong>{proposal.requested_term}</strong><small>{proposal.domain_pack ? `${proposal.domain_pack.pack_id} · ${proposal.domain_pack.version}` : "手工候选"}</small></span>
              <span className={`metric-proposal-status ${proposal.status}`}>{proposal.status}</span>
            </div>
            <div className="metric-candidate-grid">
              {proposal.candidates.map((candidate) => (
                <CandidateCard
                  key={candidate.candidate_id}
                  api={api}
                  proposal={proposal}
                  candidate={candidate}
                  busy={busy}
                  onBusy={setBusy}
                  onError={setError}
                  onUpdated={(updated) => setProposals((items) => replaceProposal(items, updated))}
                />
              ))}
            </div>
            {reports[proposal.proposal_id] && (
              <div className="metric-validation-result">
                <strong><FlaskConical size={14} />确定性校验</strong>
                {reports[proposal.proposal_id].issues.length === 0
                  ? <span className="success-copy"><CheckCircle2 size={14} />字段、类型、时间语义与粒度检查通过</span>
                  : reports[proposal.proposal_id].issues.map((issue) => <span key={issue.code} className={issue.severity}>{issue.code} · {issue.message}</span>)}
              </div>
            )}
            <div className="metric-proposal-actions">
              <button className="secondary-action" disabled={!proposal.selected_candidate_id || proposal.candidates.some((item) => item.candidate_id === proposal.selected_candidate_id && item.required_decisions.length > 0) || busy !== "" || proposal.status === "pending_approval" || proposal.status === "approved"} onClick={() => void validate(proposal)}>运行校验</button>
              {overlaysEnabled && conversationId && proposal.status === "pending_approval" && (
                <button className="secondary-action" disabled={!reports[proposal.proposal_id] || busy !== "" || overlayProposals[proposal.proposal_id]} onClick={() => void useInConversation(proposal)}>{overlayProposals[proposal.proposal_id] ? "已绑定当前会话" : "临时用于当前会话"}</button>
              )}
              <button className="primary-action" disabled={proposal.status !== "pending_approval" || !reports[proposal.proposal_id] || busy !== ""} onClick={() => void approve(proposal)}>管理员批准并激活</button>
            </div>
          </article>
        ))}
        {proposals.length === 0 && <small className="evidence-empty">尚无指标提案</small>}
      </div>
    </section>
  );
}

function CandidateCard({ api, proposal, candidate, busy, onBusy, onError, onUpdated }: {
  api: ApiClient;
  proposal: MetricProposal;
  candidate: MetricProposalCandidate;
  busy: string;
  onBusy: (value: string) => void;
  onError: (value: string) => void;
  onUpdated: (proposal: MetricProposal) => void;
}) {
  const definition = candidate.definition;
  const [currency, setCurrency] = useState(definition.currency ?? "BRL");
  const [timeRef, setTimeRef] = useState(definition.default_time_ref ?? "");
  const [statusRef, setStatusRef] = useState(definition.scope.status_ref ?? "");
  const [statuses, setStatuses] = useState(definition.scope.included_statuses.join(", "));
  const [refund, setRefund] = useState(definition.scope.refund_treatment);
  const [refundRef, setRefundRef] = useState(definition.scope.refund_ref ?? "");
  const [notes, setNotes] = useState(definition.scope.notes ?? "");
  const [confirmed, setConfirmed] = useState(false);
  const isSelected = proposal.selected_candidate_id === candidate.candidate_id;
  const refundNeedsField = refund === "exclude_refunded" || refund === "net_of_refunds";
  const canConfirm = currency.trim() && timeRef.trim() && notes.trim() && confirmed && (!refundNeedsField || refundRef.trim());
  const fieldRefs = useMemo(() => formulaText(definition.formula), [definition.formula]);

  async function select() {
    onBusy(`select:${candidate.candidate_id}`); onError("");
    try { onUpdated(await api.selectMetricCandidate(proposal.proposal_id, candidate.candidate_id, proposal.revision)); }
    catch (reason) { onError(reason instanceof Error ? reason.message : "候选选择失败"); }
    finally { onBusy(""); }
  }

  async function confirmScope() {
    if (!canConfirm) return;
    const included = statuses.split(",").map((item) => item.trim()).filter(Boolean);
    const updated: MetricProposalCandidate = {
      ...candidate,
      required_decisions: [],
      definition: {
        ...definition,
        currency: currency.trim(),
        default_time_ref: timeRef.trim(),
        allowed_time_refs: Array.from(new Set([timeRef.trim(), ...definition.allowed_time_refs])),
        default_filter: statusRef.trim() && included.length > 0 ? {
          kind: "set", operation: "in", operand: { kind: "field", ref: statusRef.trim() }, values: included,
        } : null,
        scope: {
          ...definition.scope,
          status_ref: statusRef.trim() || null,
          included_statuses: included,
          refund_treatment: refund,
          refund_ref: refundNeedsField ? refundRef.trim() : null,
          notes: notes.trim(),
        },
      },
    };
    onBusy(`revise:${candidate.candidate_id}`); onError("");
    try { onUpdated(await api.reviseMetricCandidate(proposal.proposal_id, updated, proposal.revision)); }
    catch (reason) { onError(reason instanceof Error ? reason.message : "口径保存失败"); }
    finally { onBusy(""); }
  }

  return (
    <div className={`metric-candidate ${isSelected ? "selected" : ""}`}>
      <div><strong>{candidate.label}</strong><small>{candidate.rationale}</small></div>
      <code>{fieldRefs}</code>
      {candidate.required_decisions.length > 0 ? (
        <div className="metric-scope-form">
          <span>待确认：{candidate.required_decisions.join(" · ")}</span>
          <label>币种<input value={currency} onChange={(event) => setCurrency(event.target.value)} /></label>
          <label>季度时间字段<input value={timeRef} onChange={(event) => setTimeRef(event.target.value)} /></label>
          <label>状态字段（可选）<input value={statusRef} onChange={(event) => setStatusRef(event.target.value)} /></label>
          <label>纳入状态（逗号分隔）<input value={statuses} onChange={(event) => setStatuses(event.target.value)} /></label>
          <label>退款处理<select value={refund} onChange={(event) => setRefund(event.target.value as typeof refund)}><option value="gross">按毛额，不抵扣退款</option><option value="not_available">数据中不可用</option><option value="exclude_refunded">排除已退款</option><option value="net_of_refunds">退款净额</option></select></label>
          {refundNeedsField && <label>退款字段<input value={refundRef} onChange={(event) => setRefundRef(event.target.value)} placeholder="例如 order.refund_amount" /></label>}
          <label>口径说明<textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="说明状态、退款、税费和时间选择" /></label>
          <label className="metric-confirm-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />我确认这些选择会改变指标结果</label>
          <button className="secondary-action" disabled={!canConfirm || busy !== ""} onClick={() => void confirmScope()}>保存口径并选择</button>
        </div>
      ) : (
        <button className="secondary-action" disabled={isSelected || busy !== ""} onClick={() => void select()}>{isSelected ? "已选择" : "选择此候选"}</button>
      )}
    </div>
  );
}
