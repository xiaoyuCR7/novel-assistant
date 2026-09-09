import { useState, type FormEvent } from "react";

import type { PlotThread, StoryNode, StoryNodeKind } from "../../lib/types";
import { NarrativeRail } from "../../components/NarrativeRail";

interface StoryMapProps {
  nodes: StoryNode[];
  plots: PlotThread[];
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
  onCreateNode: (node: {
    kind: StoryNodeKind;
    title: string;
    parent_id: string | null;
    order_index: number;
  }) => Promise<void>;
  onCreatePlot?: (plot: {
    kind: PlotThread["kind"];
    title: string;
    promise: string;
    status: string;
    start_node_id: string | null;
  }) => Promise<void>;
}

export function StoryMap({
  nodes,
  plots,
  selectedNodeId,
  onSelectNode,
  onCreateNode,
  onCreatePlot,
}: StoryMapProps) {
  const [kind, setKind] = useState<StoryNodeKind>("chapter");
  const [title, setTitle] = useState("");
  const [plotKind, setPlotKind] = useState<PlotThread["kind"]>("main");
  const [plotTitle, setPlotTitle] = useState("");
  const [promise, setPromise] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const selected = nodes.find(node => node.id === selectedNodeId);
  const parentId = kind === "volume" ? null
    : kind === "chapter" && selected?.kind === "chapter" ? selected.parent_id ?? null
    : selected?.id ?? null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await onCreateNode({
        kind,
        title,
        parent_id: parentId,
        order_index: nodes.filter((node) => node.kind === kind).length + 1,
      });
      setTitle("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function submitPlot(event: FormEvent) {
    event.preventDefault();
    if (!onCreatePlot) return;
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await onCreatePlot({
        kind: plotKind,
        title: plotTitle,
        promise,
        status: "active",
        start_node_id: selected?.id ?? null,
      });
      setPlotTitle("");
      setPromise("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="studio-section">
      <header role="group" className="section-heading">
        <div>
          <span className="eyebrow">故事地图</span>
          <h2>结构与承诺</h2>
        </div>
        <p>每个节点都应该改变人物、信息或风险。</p>
      </header>
      <div className="story-map-grid">
        <div className="node-board">
          {nodes.map((node) => (
            <button
              key={node.id}
              type="button"
              aria-label={node.title}
              aria-current={selectedNodeId === node.id ? "page" : undefined}
              className={`story-card story-card--${node.kind}`}
              onClick={() => onSelectNode(node.id)}
            >
              <small>{node.kind}</small>
              <strong>{node.title}</strong>
              <span>{node.status}</span>
            </button>
          ))}
        </div>
        <NarrativeRail nodes={nodes} plots={plots} />
      </div>
      <form className="inline-creator" onSubmit={submit}>
        <label>
          节点类型
          <select
            aria-label="节点类型"
            disabled={busy}
            value={kind}
            onChange={(event) => setKind(event.target.value as StoryNodeKind)}
          >
            <option value="volume">卷</option>
            <option value="chapter">章</option>
            <option value="scene">场景</option>
          </select>
        </label>
        <label>
          节点标题
          <input
            aria-label="节点标题"
            disabled={busy}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={busy}>
          加入故事地图
        </button>
      </form>
      {onCreatePlot && (
        <form className="inline-creator plot-creator" onSubmit={submitPlot}>
          <label>
            情节类型
            <select
              aria-label="情节类型"
              disabled={busy}
              value={plotKind}
              onChange={(event) =>
                setPlotKind(event.target.value as PlotThread["kind"])
              }
            >
              <option value="main">主线</option>
              <option value="subplot">支线</option>
              <option value="character">人物弧</option>
              <option value="foreshadowing">伏笔</option>
            </select>
          </label>
          <label>
            情节标题
            <input
              aria-label="情节标题"
              disabled={busy}
              value={plotTitle}
              onChange={(event) => setPlotTitle(event.target.value)}
              required
            />
          </label>
          <label>
            叙事承诺
            <input
              aria-label="叙事承诺"
              disabled={busy}
              value={promise}
              onChange={(event) => setPromise(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            加入叙事轨
          </button>
        </form>
      )}
      {error && (
        <p role="alert" className="error-note">
          {error}
        </p>
      )}
    </section>
  );
}
