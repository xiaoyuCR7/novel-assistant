import type { ImportAnalysis } from "../../lib/types";

const statusLabels: Record<ImportAnalysis["status"], string> = {
  imported: "等待分析",
  analyzing: "正在分析",
  paused: "已暂停",
  partially_analyzed: "部分完成",
  analyzed: "分析完成",
  analysis_failed: "分析失败",
};

function safely(action: () => unknown) {
  try {
    void Promise.resolve(action()).catch(() => undefined);
  } catch {
    // The project studio owns and renders analysis request errors.
  }
}

export function ImportAnalysisPanel({
  analysis,
  busy,
  onPause,
  onContinue,
  onRetry,
}: {
  analysis: ImportAnalysis;
  busy: boolean;
  onPause: (revision: number) => unknown;
  onContinue: (revision: number) => unknown;
  onRetry: (revision: number) => unknown;
}) {
  const progress = analysis.progress;
  return (
    <section className="import-analysis-panel" aria-labelledby="import-analysis-title">
      <header className="section-heading">
        <div>
          <span className="eyebrow">导入分析</span>
          <h3 id="import-analysis-title">{statusLabels[analysis.status]}</h3>
        </div>
        <strong>已分析 {progress.completed}/{progress.total}</strong>
      </header>
      <progress max={Math.max(progress.total, 1)} value={progress.completed} aria-label="导入分析进度" />
      <p>排队 {progress.queued} · 处理中 {progress.running} · 失败 {progress.failed}</p>
      {analysis.current_unit && <p>当前：{analysis.current_unit.unit_key}</p>}
      {analysis.last_error && <p role="alert" className="error-note">{analysis.last_error}</p>}
      <div className="inline-actions">
        {analysis.status === "imported" && <button type="button" disabled={busy} onClick={() => safely(() => onContinue(analysis.revision))}>开始分析</button>}
        {analysis.status === "analyzing" && <button type="button" disabled={busy} onClick={() => safely(() => onPause(analysis.revision))}>暂停分析</button>}
        {analysis.status === "paused" && <button type="button" disabled={busy} onClick={() => safely(() => onContinue(analysis.revision))}>继续分析</button>}
        {(analysis.status === "analysis_failed" || analysis.status === "partially_analyzed") && (
          <button type="button" disabled={busy} onClick={() => safely(() => onRetry(analysis.revision))}>重试失败项</button>
        )}
      </div>
    </section>
  );
}

export function ImportAnalysisCard({
  analysis,
  pendingCount,
  onOpen,
}: {
  analysis: ImportAnalysis;
  pendingCount: number;
  onOpen: () => void;
}) {
  return (
    <section className="import-analysis-card" aria-label="导入分析摘要">
      <div>
        <span className="eyebrow">导入分析 · {statusLabels[analysis.status]}</span>
        <strong>已分析 {analysis.progress.completed}/{analysis.progress.total}</strong>
      </div>
      <button
        type="button"
        className="secondary-action"
        aria-label={pendingCount > 0 ? "审核待确认记忆" : "查看导入分析"}
        onClick={onOpen}
      >
        {pendingCount > 0 ? `待确认记忆 ${pendingCount}` : "查看导入分析"}
      </button>
    </section>
  );
}
