import type { StoryNode } from "../lib/types";

interface BookSpineProps {
  nodes: StoryNode[];
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
}

const kindLabel = { volume: "卷", chapter: "章", scene: "场" } as const;

export function BookSpine({
  nodes,
  selectedNodeId,
  onSelectNode,
}: BookSpineProps) {
  return (
    <nav className="book-spine" aria-label="书脊轨道">
      <div className="rail-heading">
        <span>叙事结构</span>
        <small>{nodes.length} 个节点</small>
      </div>
      <ol className="spine-list">
        {nodes.map((node) => (
          <li key={node.id} className={`spine-node spine-node--${node.kind}`}>
            <span className="spine-thread" aria-hidden="true" />
            <button
              type="button"
              aria-label={node.title}
              aria-current={selectedNodeId === node.id ? "page" : undefined}
              onClick={() => onSelectNode(node.id)}
            >
              <span className="node-kind">{kindLabel[node.kind]}</span>
              <span className="node-copy">
                <strong>{node.title}</strong>
                <small>
                  {{
                    planned: "待创作",
                    drafting: "创作中",
                    completed: "已完成",
                    summary_pending: "待总结",
                  }[node.status] ?? node.status}
                </small>
              </span>
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}
