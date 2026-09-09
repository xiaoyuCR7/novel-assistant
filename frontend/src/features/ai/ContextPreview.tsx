interface ContextFragment {
  source_type: string;
  source_id: string;
  reason: string;
  hard: boolean;
  estimated_tokens: number;
}

export interface ContextSnapshot {
  token_budget?: number;
  total_estimated_tokens?: number;
  fragments?: ContextFragment[];
  dropped_source_ids?: string[];
  stage_inputs?: Record<string, {
    estimated_input_tokens: number;
    included_sources: Array<{ source_type: string; source_id: string }>;
    dropped_source_ids: string[];
  }>;
}

export function ContextPreview({ context }: { context: ContextSnapshot }) {
  const droppedSourceIds = context.dropped_source_ids ?? [];
  const hasTokenStats = typeof context.total_estimated_tokens === "number"
    && typeof context.token_budget === "number";
  const hasFragments = (context.fragments?.length ?? 0) > 0;
  const hasStageInputs = !!context.stage_inputs && Object.keys(context.stage_inputs).length > 0;
  if (!hasTokenStats && !hasFragments && !hasStageInputs && droppedSourceIds.length === 0)
    return <p role="status">上下文尚未组装。</p>;
  return (
    <section className="context-preview">
      <header role="group">
        <strong>检索候选与硬约束</strong>
        {hasTokenStats && (
          <span>
            {context.total_estimated_tokens} / {context.token_budget} tokens
          </span>
        )}
      </header>
      {droppedSourceIds.length > 0 && (
        <aside className="context-truncation" role="status">
          <strong>上下文已截断</strong>
          <span>为适应预算省略 {droppedSourceIds.length} 项。</span>
          <details>
            <summary>查看省略来源</summary>
            <ul>
              {droppedSourceIds.map((sourceId) => (
                <li key={sourceId}><code>{sourceId}</code></li>
              ))}
            </ul>
          </details>
        </aside>
      )}
      {context.stage_inputs && (
        <details>
          <summary>各阶段实际输入（估算）</summary>
          {Object.entries(context.stage_inputs).map(([stage, input]) => (
            <p key={stage}>
              {stage}：约 {input.estimated_input_tokens} tokens · 已用 {input.included_sources.length} 项参考 ·
              为预算省略 {input.dropped_source_ids.length} 项软参考
            </p>
          ))}
        </details>
      )}
      <ol>
        {(context.fragments ?? []).map((fragment) => (
          <li key={`${fragment.source_type}-${fragment.source_id}`}>
            <span
              className={`constraint-badge ${fragment.hard ? "is-hard" : ""}`}
            >
              {fragment.hard ? "硬约束" : "参考"}
            </span>
            <div>
              <strong>{fragment.reason}</strong>
              <small>
                {fragment.source_type} · 约 {fragment.estimated_tokens} tokens
              </small>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
