import type { ContextFragment, LibraryItem, LibrarySummary, MaterialReference } from "../../lib/types";
import { useState } from "react";
import { Icon } from "../../components/Icon";
import { categoryNames } from "../library/MaterialEditorSheet";

const sourceTypes: Record<string, string> = { story_entity: 'entity', canon_fact: 'canon',
  timeline_event: 'timeline', plot_thread: 'plot', style_profile: 'style',
  chapter_summary: 'summary', previous_end_state: 'summary' };
function fragmentReference(fragment: ContextFragment): MaterialReference | null {
  if (['project_core', 'chapter_contract', 'current_draft'].includes(fragment.source_type)) return null;
  const type = fragment.citation?.type ?? sourceTypes[fragment.source_type] ?? fragment.source_type;
  const id = fragment.citation?.id ?? fragment.source_id;
  return categoryNames[type] && id ? { type, id } : null;
}
export function ContextLens({
  fragments,
  references,
  selected,
  onOpen,
  onPin,
  onEdit,
  onClose,
  detailPending = false, detailError, hasSelection = false, onRetry,
  ledgerLoading = false, ledgerError, onRetryLedger, ledgerHasMore = false, onLoadMoreLedger,
}: {
  fragments: ContextFragment[];
  references: LibrarySummary[];
  selected: LibraryItem | null;
  onOpen: (item: MaterialReference) => void;
  onPin: (item: LibraryItem) => void;
  onEdit: (item: LibraryItem) => void;
  onClose: () => void;
  detailPending?: boolean;
  detailError?: string;
  hasSelection?: boolean;
  onRetry?: () => void;
  ledgerLoading?: boolean;
  ledgerError?: string;
  onRetryLedger?: () => void;
  ledgerHasMore?: boolean;
  onLoadMoreLedger?: () => void;
}) {
  const [copyNote, setCopyNote] = useState("");
  const relativePath = selected && typeof selected.record.relative_path === "string"
    ? selected.record.relative_path
    : null;
  return (
    <div className="context-lens">
      <header role="group" className="lens-heading">
        <div>
          <span className="utility-label">与你的故事保持一致</span>
          <h2>创作参考</h2>
        </div>
        <Icon name="book" />
      </header>
      {!selected && hasSelection && <section className="reference-detail">
        <button className="icon-button" aria-label="关闭素材预览" onClick={onClose}><Icon name="close" size={15} /></button>
        {detailPending && <p role="status">正在加载素材详情…</p>}
        {detailError && <p role="alert">{detailError}<button onClick={onRetry}>重试素材详情</button></p>}
      </section>}
      {selected ? (
        <section className="reference-detail">
          <div className="reference-meta">
            <span className="badge">{categoryNames[selected.type]}</span>
            <button
              className="icon-button"
              aria-label="关闭素材预览"
              onClick={onClose}
            >
              <Icon name="close" size={15} />
            </button>
          </div>
          <h3>{selected.title}</h3>
          {selected.type === 'summary' && selected.status !== 'valid' && <p role="status">历史总结（{selected.status}），不属于当前有效账本。</p>}
          {relativePath && <p className="subtle">来源路径：<code>{relativePath}</code></p>}
          <p className="reference-body">{selected.content}</p>
          <div className="reference-actions">
            {selected.type !== "manuscript" && (
              <button onClick={() => onPin(selected)}>
                <Icon name="pin" size={14} />
                {selected.is_pinned ? "取消固定" : "固定素材"}
              </button>
            )}
            <button onClick={() => onEdit(selected)}>
              {selected.type === "manuscript" ? "查看章节" : "编辑素材"}
            </button>
            <button
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(
                    `[[ref:${selected.type}:${selected.id}]]`,
                  );
                  setCopyNote("引用已复制，可粘贴到正文或创作要求。");
                } catch {
                  setCopyNote(
                    `引用标记：[[ref:${selected.type}:${selected.id}]]`,
                  );
                }
              }}
            >
              复制引用
            </button>
          </div>
          {copyNote && (
            <p role="status" className="subtle">
              {copyNote}
            </p>
          )}
          <p className="subtle">修订 {selected.revision} · 仅限当前项目</p>
        </section>
      ) : !hasSelection ? (
        <div className="context-intro">
          <span className="status-ring">
            <Icon name="shield" size={22} />
          </span>
          <strong>让前文成为下一章的依据</strong>
          <p>设定、伏笔与章节总结，在需要时出现。</p>
        </div>
      ) : null}
      <div className="sidebar-caption">
        本次上下文 <span>{fragments.length} 项</span>
      </div>
      {fragments.length ? (
        fragments.map((f, index) => (
          <button
            className="context-fragment"
            key={`${f.source_id}:${index}`}
            disabled={!fragmentReference(f)}
            onClick={() => {
              const source = fragmentReference(f);
              if (source) onOpen(source);
            }}
          >
            <span className={`constraint-dot ${f.hard ? "hard" : ""}`} />
            <span>
              <strong>
                {f.citation?.title ??
                  {
                    chapter_contract: "章节契约",
                    project_core: "故事核心",
                    chapter_summary: "前章总结",
                    previous_end_state: "上一章章末状态",
                  }[f.source_type] ??
                  f.source_type}
              </strong>
              <small>{f.reason}</small>
              <em>
                {f.hard ? "硬约束" : "软参考"} · {f.estimated_tokens} tokens
                {f.channel ? ` · ${f.channel}` : ""}
              </em>
            </span>
          </button>
        ))
      ) : (
        <p className="sidebar-hint">开始生成后，可在这里查看实际引用与取舍。</p>
      )}
      <div className="sidebar-caption">
        连续性账本 <Icon name="clock" size={12} />
      </div>
      {references
        .filter((i) => i.type === "summary" && i.status === "valid")
        .map((i) => (
          <button
            className="ledger-preview"
            key={i.id}
            onClick={() => onOpen(i)}
          >
            <span className="badge">
              {i.origin === "author_edited" ? "作者已编辑" : "AI 总结"}
            </span>
            <strong>{i.title}</strong>
            <p>{i.preview}</p>
          </button>
        ))}
      {ledgerLoading && <p role="status">正在加载连续性账本…</p>}
      {ledgerError && <p role="alert">账本加载失败：{ledgerError}<button onClick={onRetryLedger}>重试连续性账本</button></p>}
      {ledgerHasMore && <button disabled={ledgerLoading} onClick={onLoadMoreLedger}>加载更多账本</button>}
    </div>
  );
}
