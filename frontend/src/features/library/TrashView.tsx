import { useState } from "react";
import type { LibrarySummary } from "../../lib/types";
import { Icon } from "../../components/Icon";
export function TrashView({
  items,
  onRestore,
  onPurge,
  loading = false, loadError, onRetry, hasMore = false, onLoadMore,
}: {
  items: LibrarySummary[];
  onRestore: (item: LibrarySummary) => Promise<void> | void;
  onPurge: (item: LibrarySummary) => Promise<void> | void;
  loading?: boolean;
  loadError?: string;
  onRetry?: () => void;
  hasMore?: boolean;
  onLoadMore?: () => void;
}) {
  const [error, setError] = useState("");
  async function act(action: () => Promise<void> | void) {
    try {
      await action();
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <section className="library-page">
      <div className="section-heading">
        <div>
          <span className="eyebrow">可恢复删除</span>
          <h2>回收站</h2>
        </div>
        <Icon name="trash" />
      </div>
      <p className="subtle">
        删除内容不会参与检索。保留 30 天；仍被引用的内容会保留到关联解除。
      </p>
      {error && <p role="alert">{error}</p>}
      {loading && <p role="status">正在加载回收站…</p>}
      {loadError && <p role="alert">回收站加载失败：{loadError}<button onClick={onRetry}>重试回收站</button></p>}
      {!loading && !loadError && !items.length && <p className="empty-note">回收站为空。</p>}
      {items.map((item) => {
        const days = Math.max(
          0,
          Math.ceil(
            (Date.parse(item.purge_after ?? "") - Date.now()) / 86400000,
          ),
        );
        return (
          <article className="trash-row" key={`${item.type}:${item.id}`}>
            <div>
              <strong>{item.title}</strong>
              <small>
                {days > 0 ? `还可恢复 ${days} 天` : "已达到清理期限"}
              </small>
            </div>
            <button
              aria-label={`恢复${item.title}`}
              onClick={() => void act(() => onRestore(item))}
            >
              恢复
            </button>
            <button
              className="danger-text"
              aria-label={`永久删除${item.title}`}
              disabled={days > 0}
              onClick={() => {
                if (window.confirm("永久删除后无法恢复。确认？"))
                  void act(() => onPurge(item));
              }}
            >
              永久删除
            </button>
          </article>
        );
      })}
      {hasMore && <button disabled={loading} onClick={onLoadMore}>加载更多回收站素材</button>}
    </section>
  );
}
