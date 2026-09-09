// Historical layout retained for compatibility tests; current studio is app/App.tsx.
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import type { StoryNode } from "../lib/types";
import { BookSpine } from "./BookSpine";
import { Icon } from "./Icon";
import { Modal } from "./Modal";
import { ResizablePane, usePaneWidth } from "./ResizablePane";

interface AppShellProps {
  projectId?: string;
  projectTitle: string;
  nodes: StoryNode[];
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
  inspector: ReactNode;
  children: ReactNode;
  sidebar?: ReactNode;
  rail?: ReactNode;
  toolbar?: ReactNode;
  referenceRequest?: number;
}
export function AppShell({
  projectId = "default",
  projectTitle,
  nodes,
  selectedNodeId,
  onSelectNode,
  inspector,
  children,
  sidebar,
  rail,
  toolbar,
  referenceRequest = 0,
}: AppShellProps) {
  const [panel, setPanel] = useState<"materials" | "context" | null>(null);
  const [contextOpen, setContextOpen] = useState(() => {
    try {
      return (
        localStorage.getItem(`studio:${projectId}:context-open`) !== "false"
      );
    } catch {
      return true;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(
        `studio:${projectId}:context-open`,
        String(contextOpen),
      );
    } catch {
      /* Layout is still usable without storage. */
    }
  }, [projectId, contextOpen]);
  useEffect(() => {
    if (referenceRequest) {
      if (window.innerWidth <= 1200) setPanel("context");
      else setContextOpen(true);
    }
  }, [referenceRequest]);
  const materials = usePaneWidth(projectId, "materials", 300, 240, 420);
  const context = usePaneWidth(projectId, "context", 320, 280, 420);
  const side = sidebar ?? (
    <BookSpine
      nodes={nodes}
      selectedNodeId={selectedNodeId}
      onSelectNode={onSelectNode}
    />
  );
  return (
    <div
      className={`studio-shell ${contextOpen ? "" : "context-collapsed"}`}
      style={
        {
          "--materials-width": `${materials.width}px`,
          "--context-width": `${context.width}px`,
        } as CSSProperties
      }
    >
      {rail}
      <div className="studio-window">
        <header className="director-header glass-surface">
          {toolbar ?? (
            <div className="brand-lockup">
              <strong>小说导演台</strong>
              <span>{projectTitle}</span>
            </div>
          )}
          <div className="header-actions">
            <span className="local-status">
              <span />
              仅存于本机
            </span>
            <button
              className="icon-button mobile-materials"
              aria-label="切换故事结构"
              aria-expanded={panel === "materials"}
              onClick={() =>
                setPanel(panel === "materials" ? null : "materials")
              }
            >
              <Icon name="panel" />
            </button>
            <button
              className="icon-button mobile-context"
              aria-label="切换上下文检查器"
              aria-expanded={panel === "context"}
              onClick={() => setPanel(panel === "context" ? null : "context")}
            >
              <Icon name="book" />
            </button>
            <button
              className="icon-button desktop-context"
              aria-label={contextOpen ? "收起参考面板" : "展开参考面板"}
              aria-expanded={contextOpen}
              onClick={() => setContextOpen(!contextOpen)}
            >
              <Icon name="panel" />
            </button>
          </div>
        </header>
        <div className="studio-grid">
          <aside
            className="materials-pane glass-surface"
            aria-label="素材侧边栏"
          >
            {side}
          </aside>
          <ResizablePane label="调整素材侧边栏宽度" pane={materials} />
          <main
            className="manuscript-stage manuscript-paper"
            aria-label="正文创作区"
          >
            {children}
          </main>
          {contextOpen && (
            <>
              <ResizablePane
                label="调整参考面板宽度"
                pane={context}
                direction={-1}
              />
              <aside
                className="context-pane glass-surface"
                aria-label="上下文检查器"
              >
                {inspector}
              </aside>
            </>
          )}
        </div>
      </div>
      {panel && (
        <Modal
          title={panel === "materials" ? "故事资料" : "上下文检查器"}
          onClose={() => setPanel(null)}
        >
          <button
            className="text-action"
            aria-label={
              panel === "materials" ? "关闭故事结构" : "关闭上下文检查器"
            }
            onClick={() => setPanel(null)}
          >
            收起面板
          </button>
          {panel === "materials" ? side : inspector}
        </Modal>
      )}
    </div>
  );
}
