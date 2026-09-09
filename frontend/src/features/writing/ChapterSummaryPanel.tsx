import { useCallback, useEffect, useRef, useState } from "react";
import type { BoundedJsonObject, ChapterSummary, GeneratedMemoryCandidateListItem, MemoryCandidate, SummaryEvidence } from "../../lib/types";
const fieldLabels: Record<string, string> = {
  recap: "章节总结",
  plot_changes: "剧情因果",
  character_states: "人物状态",
  knowledge_boundaries: "知识边界",
  world_changes: "世界变化",
  open_threads: "未解伏笔",
  end_state: "章末状态",
  fact_candidates: "待确认事实",
};
const candidateKindLabels: Record<GeneratedMemoryCandidateListItem["kind"], string> = {
  canon: "世界事实",
  entity_state: "人物状态",
  timeline: "时间线",
  plot: "剧情线",
};

type CandidateEvidence = GeneratedMemoryCandidateListItem["evidence"];
type CandidateEdit = (
  id: string,
  payload: BoundedJsonObject,
  evidence: CandidateEvidence,
  revision: number,
) => Promise<unknown>;
type CandidateDecision = (id: string, revision: number) => Promise<unknown>;

function candidateErrorMessage(error: unknown) {
  const code = typeof error === "object" && error !== null && "code" in error
    ? String((error as { code?: unknown }).code ?? "")
    : "";
  const message = error instanceof Error ? error.message : String(error);
  if (code === "LEGACY_STATE_TRANSITION_REQUIRED" || message.includes("LEGACY_STATE_TRANSITION_REQUIRED")) {
    return '该人物仍有旧版全局状态。请编辑 payload，加入 legacy_transition: "baseline" 或 "retire" 后再确认。';
  }
  return message;
}

function isGeneratedCandidate(candidate: MemoryCandidate): candidate is GeneratedMemoryCandidateListItem {
  return candidate.origin === "generated";
}

function CandidateCard({
  candidate,
  busy,
  onEdit,
  onConfirm,
  onReject,
  onStateChange,
}: {
  candidate: GeneratedMemoryCandidateListItem;
  busy: boolean;
  onEdit: CandidateEdit;
  onConfirm: CandidateDecision;
  onReject: CandidateDecision;
  onStateChange: (id: string, field: "dirty" | "pending", value: boolean) => void;
}) {
  const label = candidateKindLabels[candidate.kind];
  const [editing, setEditing] = useState(false);
  const [payloadText, setPayloadText] = useState(() => JSON.stringify(candidate.payload, null, 2));
  const [quote, setQuote] = useState(candidate.evidence.quote);
  const [start, setStart] = useState(String(candidate.evidence.start));
  const [end, setEnd] = useState(String(candidate.evidence.end));
  const [editBaseline, setEditBaseline] = useState(() => ({
    revision: candidate.revision,
    payloadText: JSON.stringify(candidate.payload, null, 2),
    quote: candidate.evidence.quote,
    start: String(candidate.evidence.start),
    end: String(candidate.evidence.end),
  }));
  const [error, setError] = useState("");
  const [decided, setDecided] = useState(false);
  const [localBusy, setLocalBusy] = useState(false);
  const pending = useRef(false);
  const blocked = busy || localBusy;
  const draftDirty = editing && (
    payloadText !== editBaseline.payloadText
    || quote !== editBaseline.quote
    || start !== editBaseline.start
    || end !== editBaseline.end
  );
  const revisionChanged = editing && candidate.revision !== editBaseline.revision;
  useEffect(() => {
    onStateChange(candidate.id, "dirty", draftDirty);
  }, [candidate.id, draftDirty, onStateChange]);
  useEffect(() => {
    onStateChange(candidate.id, "pending", localBusy);
  }, [candidate.id, localBusy, onStateChange]);
  if (decided) return null;

  async function act(operation: () => Promise<unknown>, hideAfter = false) {
    if (busy || pending.current) return;
    pending.current = true;
    setLocalBusy(true);
    setError("");
    try {
      await operation();
      if (hideAfter) setDecided(true);
    } catch (cause) {
      setError(candidateErrorMessage(cause));
    } finally {
      pending.current = false;
      setLocalBusy(false);
    }
  }

  function beginEdit() {
    const baseline = {
      revision: candidate.revision,
      payloadText: JSON.stringify(candidate.payload, null, 2),
      quote: candidate.evidence.quote,
      start: String(candidate.evidence.start),
      end: String(candidate.evidence.end),
    };
    setPayloadText(baseline.payloadText);
    setQuote(baseline.quote);
    setStart(baseline.start);
    setEnd(baseline.end);
    setEditBaseline(baseline);
    setError("");
    setEditing(true);
  }

  async function saveEdit() {
    let payload: unknown;
    try {
      payload = JSON.parse(payloadText);
    } catch {
      setError("payload 必须是有效的 JSON 对象。");
      return;
    }
    const cleanStart = start.trim();
    const cleanEnd = end.trim();
    if (!cleanStart || !cleanEnd) {
      setError("证据原文不能为空，且终点必须大于非负起点。");
      return;
    }
    const evidence = { quote, start: Number(cleanStart), end: Number(cleanEnd) };
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      setError("payload 必须是 JSON 对象。");
      return;
    }
    if (!quote || !Number.isSafeInteger(evidence.start) || !Number.isSafeInteger(evidence.end)
      || evidence.start < 0 || evidence.end <= evidence.start) {
      setError("证据原文不能为空，且终点必须大于非负起点。");
      return;
    }
    await act(async () => {
      await onEdit(candidate.id, payload as BoundedJsonObject, evidence, editBaseline.revision);
      setEditing(false);
    });
  }

  return (
    <article className="memory-candidate-card">
      <div className="summary-heading">
        <span className="badge">{label}</span>
        <small>修订 {candidate.revision}</small>
      </div>
      {editing ? (
        <div className="memory-candidate-editor">
          <label>
            {label}候选 payload JSON
            <textarea
              aria-label={`${label}候选 payload JSON`}
              rows={8}
              value={payloadText}
              onChange={(event) => setPayloadText(event.target.value)}
            />
          </label>
          <label>
            {label}候选证据原文
            <textarea
              aria-label={`${label}候选证据原文`}
              rows={3}
              value={quote}
              onChange={(event) => setQuote(event.target.value)}
            />
          </label>
          <div className="memory-candidate-offsets">
            <label>
              {label}候选证据起点
              <input
                aria-label={`${label}候选证据起点`}
                type="number"
                min={0}
                value={start}
                onChange={(event) => setStart(event.target.value)}
              />
            </label>
            <label>
              {label}候选证据终点
              <input
                aria-label={`${label}候选证据终点`}
                type="number"
                min={1}
                value={end}
                onChange={(event) => setEnd(event.target.value)}
              />
            </label>
          </div>
          <div className="memory-candidate-actions">
            <button disabled={blocked} onClick={() => void saveEdit()}>保存{label}候选</button>
            <button disabled={blocked} onClick={() => { setEditing(false); setError(""); }}>取消编辑</button>
          </div>
          {revisionChanged && (
            <div>
              <p role="alert">候选已在其他请求中更新。你的编辑已保留，请确认新基线后再保存。</p>
              <button disabled={blocked} onClick={() => {
                setEditBaseline((baseline) => ({ ...baseline, revision: candidate.revision }));
                setError("");
              }}>保留草稿并以当前候选为基线</button>
            </div>
          )}
        </div>
      ) : (
        <>
          <pre>{JSON.stringify(candidate.payload, null, 2)}</pre>
          <blockquote>{candidate.evidence.quote}</blockquote>
          <small>证据位置按 Unicode 码点计算：{candidate.evidence.start}–{candidate.evidence.end}</small>
          <div className="memory-candidate-actions">
            <button disabled={blocked} onClick={beginEdit}>编辑{label}候选</button>
            <button disabled={blocked} onClick={() => {
              if (!window.confirm("确认后将更新权威故事记忆，是否继续？")) return;
              void act(() => onConfirm(candidate.id, candidate.revision), true);
            }}>确认{label}候选</button>
            <button disabled={blocked} onClick={() => {
              void act(() => onReject(candidate.id, candidate.revision), true);
            }}>拒绝{label}候选</button>
          </div>
        </>
      )}
      {error && !revisionChanged && <p role="alert">{error}</p>}
    </article>
  );
}
function editableDetails(details: ChapterSummary["details"]): Record<string, string | string[]> {
  return Object.fromEntries(Object.entries(details).filter(
    (entry): entry is [string, string | string[]] => entry[0] !== "evidence" &&
      (typeof entry[1] === "string" || (Array.isArray(entry[1]) &&
        entry[1].every(value => typeof value === "string"))),
  ));
}
export function ChapterSummaryPanel({
  summary,
  onSave,
  saving = false,
  onDirtyChange,
  deleted = false,
  onDiscard,
  candidates,
  candidatesLoading = false,
  candidateError = "",
  candidateBusyId = null,
  hasMoreCandidates = false,
  loadingMoreCandidates = false,
  onLoadMoreCandidates,
  onRetryCandidates,
  onEditCandidate,
  onConfirmCandidate,
  onRejectCandidate,
  onCandidatePendingChange,
}: {
  summary: ChapterSummary;
  saving?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  deleted?: boolean;
  onDiscard?: () => void;
  candidates?: MemoryCandidate[];
  candidatesLoading?: boolean;
  candidateError?: string;
  candidateBusyId?: string | null;
  hasMoreCandidates?: boolean;
  loadingMoreCandidates?: boolean;
  onLoadMoreCandidates?: () => void;
  onRetryCandidates?: () => void;
  onEditCandidate?: CandidateEdit;
  onConfirmCandidate?: CandidateDecision;
  onRejectCandidate?: CandidateDecision;
  onCandidatePendingChange?: (pending: boolean) => void;
  onSave: (
    recap: string,
    revision: number,
    details: ChapterSummary["details"],
    summaryId: string,
  ) => Promise<ChapterSummary | void>;
}) {
  const [editing, setEditing] = useState(false),
    [recap, setRecap] = useState(summary.recap),
    [error, setError] = useState("");
  const [details, setDetails] = useState(() => editableDetails(summary.details)),
    [busy, setBusy] = useState(false);
  const [baseline, setBaseline] = useState(summary);
  const [candidateStates, setCandidateStates] = useState<Record<string, { dirty: boolean; pending: boolean }>>({});
  const generatedCandidates = candidates?.filter(isGeneratedCandidate);
  const pending = useRef(false);
  const draft = JSON.stringify([recap, { ...details, recap }]);
  const currentDraft = useRef(draft);
  const currentlyDeleted = useRef(deleted);
  useEffect(() => { currentDraft.current = draft; }, [draft]);
  useEffect(() => { currentlyDeleted.current = deleted; }, [deleted]);
  const dirty = editing && (deleted || draft !== JSON.stringify([baseline.recap, { ...editableDetails(baseline.details), recap: baseline.recap }]));
  const candidateDirty = generatedCandidates?.some((candidate) => candidateStates[candidate.id]?.dirty) ?? false;
  const candidatePending = generatedCandidates?.some((candidate) => candidateStates[candidate.id]?.pending) ?? false;
  const identityChanged = summary.id !== baseline.id;
  const evidenceSummary = editing ? baseline : summary;
  const evidence = evidenceSummary.details.evidence;
  const citations = (Array.isArray(evidence) ? evidence : []).filter(
    (item): item is SummaryEvidence => typeof item === "object" && item !== null,
  );
  const candidateStateChange = useCallback((
    id: string,
    field: "dirty" | "pending",
    value: boolean,
  ) => {
    setCandidateStates((current) => {
      const previous = current[id] ?? { dirty: false, pending: false };
      if (previous[field] === value) return current;
      return { ...current, [id]: { ...previous, [field]: value } };
    });
  }, []);
  useEffect(() => { onDirtyChange?.(dirty || candidateDirty); }, [dirty, candidateDirty, onDirtyChange]);
  useEffect(() => { onCandidatePendingChange?.(candidatePending); }, [candidatePending, onCandidatePendingChange]);
  return (
    <section className="summary-panel">
      <div className="summary-heading">
        <span className="badge">
          {summary.origin === "author_edited" ? "作者已编辑" : "AI 总结"}
        </span>
        <span>
          {deleted ? "总结已删除"
            : summary.status === "valid"
            ? "已加入连续性检索"
            : "正文已修改 · 总结已过期"}
        </span>
      </div>
      {!deleted && summary.ledger_pending && <p role="status">总结已保存 · 账本待修复，可在参考资料中仅修复本地账本。</p>}
      {deleted && (
        <div>
          <p role="alert">总结已删除。你的本地草稿已保留，可选中复制；保存已禁用，不会自动恢复被删除的总结。</p>
          {onDiscard && <button disabled={saving || busy} onClick={() => {
            if (window.confirm("确定放弃已删除总结的本地草稿？请先复制需要保留的内容。")) onDiscard();
          }}>放弃已删除总结的本地草稿</button>}
        </div>
      )}
      {summary.provider === "demo" && (
        <p className="demo-notice">
          离线演示摘录 · 非真实模型推理。连接本机模型后可生成结构化总结。
        </p>
      )}
      {editing ? (
        <>
          <textarea
            aria-label="章节总结"
            value={recap}
            onChange={(e) => setRecap(e.target.value)}
            rows={6}
          />
          <details>
            <summary>修订结构化记录</summary>
            <p className="subtle">列表每行一条，保存后更新连续性检索。</p>
            {Object.entries(details)
              .filter(([key]) => key !== "recap")
              .map(([key, value]) => (
                <label key={key}>
                  {fieldLabels[key] ?? key}
                  <textarea
                    aria-label={fieldLabels[key] ?? key}
                    rows={3}
                    value={Array.isArray(value) ? value.join("\n") : value}
                    onChange={(e) =>
                      setDetails({
                        ...details,
                        [key]: Array.isArray(value)
                          ? e.target.value.split("\n")
                          : e.target.value,
                      })
                    }
                  />
                </label>
              ))}
          </details>
          <button
            disabled={saving || busy || deleted || identityChanged || !recap.trim()}
            onClick={async () => {
              if (saving || pending.current || deleted || identityChanged) return;
              pending.current = true;
              setBusy(true);
              setError("");
              const submitted = draft;
              try {
                const saved = await onSave(recap, baseline.revision, { ...details, recap }, baseline.id);
                setBaseline(saved ?? { ...baseline, recap, details: { ...details, recap } });
                if (currentDraft.current === submitted && !currentlyDeleted.current) setEditing(false);
              } catch (e) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                pending.current = false;
                setBusy(false);
              }
            }}
          >
            保存总结
          </button>
        </>
      ) : (
        <>
          <p>{summary.recap}</p>
          <button
            disabled={deleted || summary.status !== "valid"}
            onClick={() => {
              setRecap(summary.recap);
              setDetails(editableDetails(summary.details));
              setBaseline(summary);
              setError("");
              setEditing(true);
            }}
          >
            编辑总结
          </button>
        </>
      )}
      {editing && !deleted && identityChanged && (
        <div>
          <p role="alert">本章已生成新总结。你的旧草稿已保留，请先确认编辑基线，避免覆盖新总结。</p>
          <button
            disabled={saving || busy || summary.status !== "valid"}
            onClick={() => {
              if (!window.confirm("保留你的草稿并以当前新总结为基线？之后保存会用这份草稿替换当前总结。")) return;
              setBaseline(summary);
              setError("");
            }}
          >以当前总结为基线继续编辑</button>
        </div>
      )}
      {editing && !deleted && !identityChanged && summary.revision !== baseline.revision && (
        <p role="alert">总结已在其他请求中更新。你的编辑已保留，保存时将校验原修订号。</p>
      )}
      {dirty && <p className="subtle">总结有未保存修改</p>}
      {error && <p role="alert">{error}</p>}
      {summary.origin === "author_edited" && <p className="subtle">作者修改未附带新的 AI 原文校验。</p>}
      {!!citations.length && (
        <details>
          <summary>原文证据（只读）</summary>
          <p className="subtle">对应生成版本：{evidenceSummary.version_id}。以下为原 AI 总结的原文依据。</p>
          {(dirty || evidenceSummary.origin === "author_edited") && <p className="subtle">作者修改未重新校验证据，不代表修改后的内容已有原文支持。</p>}
          {citations.map((citation, index) => (
            <div key={index}>
              <strong>{fieldLabels[citation.field] ?? citation.field} · 第 {citation.index + 1} 项</strong>
              <small>原文字符位置 {citation.start}–{citation.end}</small>
              <blockquote>{citation.quote}</blockquote>
            </div>
          ))}
        </details>
      )}
      <details>
        <summary>查看结构化连续性记录</summary>
        {Object.entries(editableDetails(summary.details))
          .filter(([k]) => k !== "recap")
          .map(([k, v]) => (
            <div key={k}>
              <strong>{fieldLabels[k] ?? k}</strong>
              <p>{Array.isArray(v) ? v.join("；") || "暂无记录" : v}</p>
            </div>
          ))}
      </details>
      {generatedCandidates !== undefined && (
        <section className="memory-candidates" aria-labelledby="memory-candidates-heading">
          <h3 id="memory-candidates-heading">待确认故事记忆</h3>
          <p className="subtle">AI 只提供有原文依据的候选；只有作者确认后才会更新权威记忆。</p>
          {candidatesLoading && !generatedCandidates.length && <p role="status">正在读取待确认记忆…</p>}
          {candidateError && (
            <p role="alert">
              {candidateError}
              {onRetryCandidates && <button onClick={onRetryCandidates}>重试候选记忆</button>}
            </p>
          )}
          {!candidatesLoading && !candidateError && !generatedCandidates.length && (
            <p>当前没有待确认的故事记忆。</p>
          )}
          {onEditCandidate && onConfirmCandidate && onRejectCandidate && generatedCandidates.map((candidate) => (
            <CandidateCard
              key={candidate.id}
              candidate={candidate}
              busy={candidateBusyId !== null}
              onEdit={onEditCandidate}
              onConfirm={onConfirmCandidate}
              onReject={onRejectCandidate}
              onStateChange={candidateStateChange}
            />
          ))}
          {hasMoreCandidates && (
            <button disabled={loadingMoreCandidates || candidateBusyId !== null} onClick={onLoadMoreCandidates}>
              {loadingMoreCandidates ? "正在加载…" : "加载更多待确认记忆"}
            </button>
          )}
        </section>
      )}
    </section>
  );
}
