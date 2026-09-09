import type { PlotThread, StoryNode } from "../lib/types";

interface NarrativeRailProps {
  nodes: StoryNode[];
  plots: PlotThread[];
}

export function NarrativeRail({ nodes, plots }: NarrativeRailProps) {
  return (
    <div className="narrative-rail" aria-label="叙事线轨">
      <div className="rail-heading">
        <span>叙事线轨</span>
        <small>{plots.length} 条线</small>
      </div>
      {plots.length === 0 ? (
        <p className="empty-note">
          还没有情节承诺。建立伏笔后，它会在章节之间留下可追踪的线。
        </p>
      ) : (
        plots.map((plot, index) => {
          const start = Math.max(
            0,
            nodes.findIndex((node) => node.id === plot.start_node_id),
          );
          const endIndex = nodes.findIndex(
            (node) => node.id === plot.due_node_id,
          );
          const end = endIndex < 0 ? nodes.length - 1 : endIndex;
          return (
            <div key={plot.id} className="plot-line">
              <span
                className="plot-line__bar"
                style={{
                  marginLeft: `${start * 7}%`,
                  width: `${Math.max(16, (end - start + 1) * 7)}%`,
                  top: `${index * 34 + 52}px`,
                }}
              />
              <strong>{plot.title}</strong>
              <small>{plot.status}</small>
            </div>
          );
        })
      )}
    </div>
  );
}
