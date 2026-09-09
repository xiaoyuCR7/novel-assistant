import { useMemo, useState } from "react";

import type {
  BoundedJson,
  BoundedJsonObject,
  ImportMemoryCandidate,
  ImportMemoryEvidence,
  MemoryCandidateBulkConfirmEntry,
} from "../../lib/types";

const kindLabels: Record<ImportMemoryCandidate["kind"], string> = {
  entity: "人物",
  relation: "关系",
  canon: "世界事实",
  timeline: "时间线",
  plot: "剧情线",
  node: "故事节点",
  node_update: "节点更新",
  style_rule: "风格规则",
  idea: "灵感",
  entity_state: "人物状态",
  chapter_summary: "章节总结",
};

const fieldLabels: Record<string, string> = {
  code: "冲突类型",
  data: "状态数据",
  description: "说明",
  differences: "差异",
  entity_name: "人物名称",
  existing_state_ids: "现有状态",
  instruction: "规则内容",
  kind: "类型",
  name: "名称",
  node_id: "故事节点",
  payload: "内容",
  predicate: "事实关系",
  record_ids: "现有记录",
  relation_type: "关系类型",
  source_entity_name: "关系起点",
  summary: "总结",
  target_entity_name: "关系终点",
  title: "标题",
  valid_from_node_id: "生效节点",
  valid_to_node_id: "失效节点",
  value: "事实内容",
};

const createSeparateKinds = new Set<ImportMemoryCandidate["kind"]>([
  "entity", "relation", "canon", "timeline", "plot", "node", "style_rule", "idea",
]);

type CandidateMutationAction = "edit" | "confirm" | "reject" | "bulk";

const actionLabels: Record<CandidateMutationAction, string> = {
  edit: "保存",
  confirm: "确认",
  reject: "拒绝",
  bulk: "批量确认",
};

function displayValue(value: BoundedJson) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function fields(value: BoundedJsonObject) {
  return (
    <dl>
      {Object.entries(value).map(([key, item]) => (
        <div key={key}>
          <dt>{fieldLabels[key] ?? key}</dt>
          <dd>{displayValue(item)}</dd>
        </div>
      ))}
    </dl>
  );
}

function evidenceQuote(item: ImportMemoryEvidence) {
  return typeof item.quote === "string" ? item.quote : "（无引用文本）";
}

function evidenceLocation(item: ImportMemoryEvidence) {
  const path = typeof item.relative_path === "string" ? item.relative_path : "导入原文";
  const start = typeof item.start === "number" ? item.start : "?";
  const end = typeof item.end === "number" ? item.end : "?";
  return `${path} · ${start}–${end}`;
}

export function MemoryCandidateReview({
  candidates,
  busyId,
  busyAction,
  onEdit,
  onConfirm,
  onReject,
  onBulkConfirm,
  onLoadMore,
  hasMore = false,
}: {
  candidates: ImportMemoryCandidate[];
  busyId: string | null;
  busyAction?: CandidateMutationAction;
  onEdit: (id: string, revision: number, payload: BoundedJsonObject, evidence: ImportMemoryEvidence[]) => unknown;
  onConfirm: (id: string, revision: number, resolution?: "create_separate") => unknown;
  onReject: (id: string, revision: number) => unknown;
  onBulkConfirm?: (entries: MemoryCandidateBulkConfirmEntry[]) => unknown;
  onLoadMore?: () => void;
  hasMore?: boolean;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [payloads, setPayloads] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [operationError, setOperationError] = useState("");
  const [separate, setSeparate] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const selectedEntries = useMemo<MemoryCandidateBulkConfirmEntry[]>(() => candidates
    .filter((item) => item.status === "pending" && selected[item.id])
    .map((item) => ({ candidate_id: item.id, revision: item.revision })), [candidates, selected]);
  const busy = busyId !== null;
  const busyCandidate = candidates.find((item) => item.id === busyId);
  const busyDescription = busy
    ? busyId === "bulk"
      ? "正在批量确认候选…"
      : `正在${busyAction ? actionLabels[busyAction] : "处理"}${busyCandidate ? kindLabels[busyCandidate.kind] : ""}候选…`
    : "";

  async function runOperation(action: () => unknown) {
    setOperationError("");
    try {
      await action();
    } catch (cause) {
      setOperationError(cause instanceof Error ? cause.message : "未知错误");
    }
  }

  return (
    <section className="memory-candidate-review" aria-labelledby="import-memory-title">
      <header className="section-heading candidate-review-heading">
        <div><span className="eyebrow">人工确认</span><h2 id="import-memory-title">待确认记忆</h2></div>
        <div className="candidate-review-tools">
          <span>{candidates.length} 条</span>
          {onBulkConfirm && (
            <button
              type="button"
              disabled={busy || selectedEntries.length === 0}
              onClick={() => void runOperation(() => onBulkConfirm(selectedEntries))}
            >批量确认已选（{selectedEntries.length}）</button>
          )}
        </div>
      </header>
      {busyDescription && <p role="status" aria-label="候选记忆操作">{busyDescription}</p>}
      {operationError && <p role="alert" className="error-note">候选记忆操作失败：{operationError}</p>}
      {candidates.length === 0 && <p>当前没有待确认或冲突记忆。</p>}
      {candidates.map((candidate) => {
        const label = kindLabels[candidate.kind];
        const canSeparate = createSeparateKinds.has(candidate.kind);
        const conflict = candidate.status === "conflict";
        const conflictCode = typeof candidate.conflict.code === "string" ? candidate.conflict.code : "";
        return (
          <article className={`memory-candidate-card ${conflict ? "memory-candidate-card--conflict" : ""}`} key={candidate.id}>
            <header>
              <label className="candidate-selector">
                <input
                  type="checkbox"
                  aria-label={`选择${label}候选`}
                  disabled={conflict || busy}
                  checked={selected[candidate.id] ?? false}
                  onChange={(event) => setSelected((current) => ({ ...current, [candidate.id]: event.target.checked }))}
                />
                <strong>{label}</strong>
              </label>
              <span>{conflict ? "需要解决冲突" : "待确认"}</span>
            </header>
            {editing === candidate.id ? (
              <label>{label} payload JSON
                <textarea
                  aria-label={`${label}候选 payload JSON`}
                  disabled={busy}
                  value={payloads[candidate.id] ?? JSON.stringify(candidate.payload, null, 2)}
                  onChange={(event) => setPayloads((current) => ({ ...current, [candidate.id]: event.target.value }))}
                />
              </label>
            ) : conflict ? (
              <div className="candidate-conflict-compare">
                <section aria-label={`${label}候选内容`}><h3>候选内容</h3>{fields(candidate.payload)}</section>
                <section aria-label={`${label}现有冲突`}><h3>现有冲突</h3>{fields(candidate.conflict)}</section>
              </div>
            ) : fields(candidate.payload)}
            {conflictCode.includes("AMBIGUOUS") && (
              <p className="candidate-guidance">请编辑候选内容中的歧义引用，明确对应的人物或节点后再确认。</p>
            )}
            <div className="candidate-evidence-list">
              {candidate.evidence.map((item, index) => (
                <figure key={`${String(item.start)}-${String(item.end)}-${index}`}>
                  <blockquote>{evidenceQuote(item)}</blockquote>
                  <figcaption>{evidenceLocation(item)}</figcaption>
                </figure>
              ))}
            </div>
            {errors[candidate.id] && <p role="alert" className="error-note">{errors[candidate.id]}</p>}
            {conflict && canSeparate && !conflictCode.includes("AMBIGUOUS") && (
              <label><input aria-label={`${label}候选创建为独立记录`} type="checkbox" disabled={busy} checked={separate[candidate.id] ?? false} onChange={(event) => setSeparate((current) => ({ ...current, [candidate.id]: event.target.checked }))} />创建为独立记录</label>
            )}
            <div className="inline-actions">
              {editing === candidate.id ? (
                <button type="button" disabled={busy} onClick={() => {
                  try {
                    const payload = JSON.parse(payloads[candidate.id] ?? JSON.stringify(candidate.payload));
                    if (!payload || Array.isArray(payload) || typeof payload !== "object") throw new Error();
                    setErrors((current) => ({ ...current, [candidate.id]: "" }));
                    void runOperation(async () => {
                      await onEdit(candidate.id, candidate.revision, payload, candidate.evidence);
                      setEditing((current) => current === candidate.id ? null : current);
                    });
                  } catch {
                    setErrors((current) => ({ ...current, [candidate.id]: "请输入有效的 JSON 对象。" }));
                  }
                }}>保存{label}候选</button>
              ) : (
                <button type="button" disabled={busy} onClick={() => {
                  setPayloads((current) => ({ ...current, [candidate.id]: JSON.stringify(candidate.payload, null, 2) }));
                  setEditing(candidate.id);
                }}>编辑{label}候选</button>
              )}
              <button
                type="button"
                disabled={busy || (conflict && !(canSeparate && separate[candidate.id]) || conflictCode.includes("AMBIGUOUS"))}
                onClick={() => void runOperation(() => onConfirm(candidate.id, candidate.revision, conflict ? "create_separate" : undefined))}
              >确认{label}候选</button>
              <button type="button" disabled={busy} onClick={() => void runOperation(() => onReject(candidate.id, candidate.revision))}>拒绝{label}候选</button>
            </div>
          </article>
        );
      })}
      {hasMore && <button type="button" disabled={busy} onClick={onLoadMore}>加载更多待确认记忆</button>}
    </section>
  );
}
