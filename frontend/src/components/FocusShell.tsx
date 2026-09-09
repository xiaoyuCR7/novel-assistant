import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { StoryNode } from "../lib/types";
import { BookSpine } from "./BookSpine";
import { Icon } from "./Icon";
import { Modal } from "./Modal";
import { ResizablePane, usePaneWidth } from "./ResizablePane";

export function FocusShell({
  projectId,
  transitionKey,
  nodes,
  selectedNodeId,
  onSelectNode,
  rail,
  toolbar,
  status,
  sidebar,
  inspector,
  children,
  referenceRequest = 0,
  onReferenceClose,
}: {
  projectId: string;
  transitionKey?: string;
  projectTitle?: string;
  nodes: StoryNode[];
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
  rail: ReactNode;
  toolbar: ReactNode;
  status?: ReactNode;
  sidebar: ReactNode;
  inspector: ReactNode;
  children: ReactNode;
  referenceRequest?: number;
  onReferenceClose?: () => void;
}) {
  const [panel, setPanel] = useState<
    "materials" | "reference" | "chapters" | null
  >(null);
  const pane = usePaneWidth(projectId, "navigation", 216, 180, 300);
  const [navigationCollapsed, setNavigationCollapsed] = useState(false);
  const contentRef = useRef<HTMLElement>(null);
  const previousPage = useRef(transitionKey);
  useLayoutEffect(() => {
    if (previousPage.current === transitionKey) return;
    previousPage.current = transitionKey;
    const content = contentRef.current;
    const motion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!content?.animate || motion?.matches) return;

    // Animate the existing container; remounting it would reset editor drafts.
    const animation = content.animate(
      [
        { opacity: 0.65, transform: "translateY(4px)" },
        { opacity: 1, transform: "translateY(0)" },
      ],
      { duration: 180, easing: "cubic-bezier(0.2, 0.65, 0.3, 1)", id: "workspace-page-switch" },
    );
    const reduceMotion = (event: MediaQueryListEvent) => {
      if (event.matches) animation.cancel();
    };
    motion?.addEventListener("change", reduceMotion);
    return () => {
      animation.cancel();
      motion?.removeEventListener("change", reduceMotion);
    };
  }, [transitionKey]);
  useEffect(() => {
    if (referenceRequest) setPanel("reference");
  }, [referenceRequest]);
  useEffect(() => {
    function key(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPanel("materials");
      }
    }
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  const chapters = (
    <>
      <button
        className="project-chat-link"
        aria-current={
          selectedNodeId === "project-chat" ||
          !nodes.some((node) => node.kind === "chapter")
            ? "page"
            : undefined
        }
        onClick={() => {
          onSelectNode("project-chat");
          setPanel(null);
        }}
      >
        <Icon name="book" size={16} />
        全书讨论
      </button>
      <BookSpine
        nodes={nodes}
        selectedNodeId={selectedNodeId}
        onSelectNode={(id) => {
          onSelectNode(id);
          setPanel(null);
        }}
      />
    </>
  );
  return (
    <div
      className={`focus-shell${navigationCollapsed ? " navigation-collapsed" : ""}`}
      style={{ "--navigation-width": pane.width + "px" } as CSSProperties}
    >
      <aside className="focus-sidebar" aria-label="小说导航">
        <div className="focus-brand">
          <span className="focus-brand-mark" aria-hidden="true"><Icon name="write" size={21} /></span>
          <div className="focus-brand-copy">小说创作室<small>为每一个故事留白</small></div>
        </div>
        {rail}
        <div className="focus-chapters">{chapters}</div>
        <button
          className="sidebar-search"
          onClick={() => setPanel("materials")}
        >
          <Icon name="search" size={16} />
          查找资料<kbd>{/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ K" : "Ctrl K"}</kbd>
        </button>
        <div className="focus-local">
          <Icon name="lock" size={12} />
          资料按小说独立保存
        </div>
      </aside>
      <ResizablePane label="调整导航栏宽度" pane={pane} />
      <div className="focus-main">
        <header className="focus-topbar">
          <div className="focus-project-tools">
            <button
              className="navigation-toggle icon-button"
              aria-label={navigationCollapsed ? "展开侧栏" : "收起侧栏"}
              aria-expanded={!navigationCollapsed}
              onClick={() => setNavigationCollapsed(value => !value)}
            >
              <Icon name="panel" size={18} />
            </button>
            {toolbar}
          </div>
          {status && <div className="focus-status">{status}</div>}
          <div className="focus-topbar-actions">
            <button
              className="mobile-chapters"
              aria-label="打开章节目录"
              onClick={() => setPanel("chapters")}
            >
              <Icon name="book" size={17} />
            </button>
            <button aria-label="参考资料" onClick={() => setPanel("materials")}>
              <Icon name="search" size={16} />
              <span>参考资料</span>
            </button>
          </div>
        </header>
        <main ref={contentRef} className="focus-content" aria-label="创作工作区">
          {children}
        </main>
      </div>
      {panel && (
        <Modal
          title={
            panel === "materials"
              ? "参考资料"
              : panel === "chapters"
                ? "章节目录"
                : "素材详情"
          }
          onClose={() => { if (panel === 'reference') onReferenceClose?.(); setPanel(null); }}
        >
          {panel === "materials"
            ? sidebar
            : panel === "chapters"
              ? chapters
              : inspector}
        </Modal>
      )}
    </div>
  );
}
