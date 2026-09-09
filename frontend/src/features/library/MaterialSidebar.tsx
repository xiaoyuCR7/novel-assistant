import { BookSpine } from "../../components/BookSpine";
import { Icon } from "../../components/Icon";
import type { LibrarySummary, MaterialReference, StoryNode } from "../../lib/types";
import { categoryNames } from "./MaterialEditorSheet";

export function MaterialSidebar({
  items,
  nodes,
  selectedNodeId,
  onSelectNode,
  onOpen,
  onCreate,
  onTrash,
  query,
  onQuery,
  category, onCategory, counts, total, pinned, loading, error, onRetry, hasMore, onLoadMore,
}: {
  items: LibrarySummary[];
  nodes: StoryNode[];
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
  onOpen: (item: MaterialReference) => void;
  onCreate: () => void;
  onTrash: () => void;
  query: string;
  onQuery: (q: string) => void;
  category: string;
  onCategory: (category: string) => void;
  counts: Record<string, number>;
  total: number;
  pinned: LibrarySummary[];
  loading: boolean;
  error?: string;
  onRetry: () => void;
  hasMore: boolean;
  onLoadMore: () => void;
}) {
  return (
    <div className="material-sidebar">
      <div className="sidebar-heading">
        <div>
          <span className="utility-label">把故事的来处放在手边</span>
          <h2>故事资料</h2>
        </div>
        <button
          className="icon-button"
          aria-label="新建素材"
          onClick={onCreate}
        >
          <Icon name="plus" />
        </button>
      </div>
      <div className="search-field">
        <Icon name="search" size={16} />
        <input
          id="material-search"
          type="search"
          aria-label="搜索本项目素材"
          placeholder="搜索本项目素材"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
        />
        <kbd>⌘ K</kbd>
      </div>
      <div className="sidebar-scroll">
        {!query && (
          <>
            <div className="sidebar-caption">
              快速访问 <Icon name="pin" size={12} />
            </div>
            {pinned.length ? (
              pinned.map((i) => (
                <button
                  className="quick-reference"
                  key={`${i.type}:${i.id}`}
                  onClick={() => onOpen(i)}
                >
                  <Icon name="book" size={15} />
                  {i.title}
                </button>
              ))
            ) : (
              <p className="sidebar-hint">打开素材后固定，常用设定随手可查。</p>
            )}
            <BookSpine
              nodes={nodes}
              selectedNodeId={selectedNodeId}
              onSelectNode={onSelectNode}
            />
          </>
        )}
        <div className="sidebar-caption">
          {query ? "搜索结果（候选命中）" : "素材分类"} <span>{total}</span>
        </div>
        <div className="category-picker">
          <select
            aria-label="筛选素材分类"
            value={category}
            onChange={(e) => onCategory(e.target.value)}
          >
            <option value="all">全部素材</option>
            {Object.entries(categoryNames).map(([k, v]) => (
              <option value={k} key={k}>
                {v} · {counts[k] ?? 0}
              </option>
            ))}
          </select>
        </div>
        <div className="material-list">
          {items.map((i) => (
            <button
              className="material-row"
              draggable
              onDragStart={(event) => {
                event.dataTransfer.setData(
                  "text/plain",
                  `[[ref:${i.type}:${i.id}]]`,
                );
                event.dataTransfer.effectAllowed = "copy";
              }}
              key={`${i.type}:${i.id}`}
              onClick={() => onOpen(i)}
            >
              <span className={`material-glyph type-${i.type}`}>
                <Icon
                  name={
                    i.type === "entity"
                      ? "people"
                      : i.type === "summary"
                        ? "clock"
                        : "book"
                  }
                  size={16}
                />
              </span>
              <span>
                <strong>{i.title}</strong>
                <small>
                  {categoryNames[i.type]}
                  {i.reason ? ` · ${i.reason}` : ""}
                </small>
              </span>
              {i.is_pinned && <Icon name="pin" size={12} />}
            </button>
          ))}
          {loading && <p role="status">正在加载素材…</p>}
          {error && <p role="alert">素材加载失败：{error}<button onClick={onRetry}>重试素材加载</button></p>}
          {hasMore && <button disabled={loading} onClick={onLoadMore}>加载更多素材</button>}
          {!loading && !error && !items.length && (
            <p className="sidebar-hint">
              {query
                ? "没有匹配的素材。试试人物名或关键词。"
                : "还没有此类素材。点上方 + 开始添加。"}
            </p>
          )}
        </div>
      </div>
      <button className="trash-shortcut" onClick={onTrash}>
        <Icon name="trash" size={16} />
        <span>回收站</span>
        <small>保留 30 天</small>
      </button>
      <div className="vault-note">
        <Icon name="lock" size={12} />
        独立资料库 · 不跨小说检索
      </div>
    </div>
  );
}
