import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { qualityApi } from '../../lib/qualityApi';
import { DiagnosticError } from '../../components/DiagnosticError';
import './quality-diff.css';

export function QualityDiffPanel({ projectId, runId, chapterId, allowPartial, disabled, onAccept }: {
  projectId: string; runId: string; chapterId: string; allowPartial: boolean; disabled: boolean;
  onAccept: (ids: string[]) => Promise<void>;
}) {
  const api = useMemo(() => qualityApi(projectId), [projectId]);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const query = useQuery({ queryKey: ['quality', projectId, 'diff', runId, chapterId], enabled: open,
    queryFn: ({ signal }) => api.diff(runId, chapterId, signal), staleTime: Infinity });
  const canSelect = allowPartial && query.data?.mode === 'polish';
  const ids = selected.filter(id => query.data?.hunks.some(hunk => hunk.id === id));
  return <section className="quality-diff-panel" aria-label="正文差异预览">
    <button type="button" aria-expanded={open} onClick={() => setOpen(current => !current)}>{open ? '收起正文差异' : '查看正文差异'}</button>
    {open && <>
      {query.isPending && <p role="status">正在比较原稿与优化稿…</p>}
      {query.error && <><DiagnosticError error={query.error} /><button type="button" onClick={() => void query.refetch()}>重试正文差异</button></>}
      {query.data && <>
        <p className="subtle">基于修订 {query.data.source_revision}。只采用勾选区块，其余保留原稿。组合稿尚未整体复核，原评分仅适用于完整优化候选。</p>
        {!canSelect && <p className="subtle">交替协作仅支持整章采纳，以保持后续章节的交接依据。</p>}
        {query.data.coarse && <p className="subtle">差异较多，本章按整体区块展示。</p>}
        {!query.data.hunks.length && <p>正文没有差异，无需重复采纳。</p>}
        {query.data.hunks.map((hunk, index) => <article key={hunk.id} className="quality-prose">
          <label><input type="checkbox" aria-label={`采用修改 ${index + 1}`} checked={ids.includes(hunk.id)} disabled={disabled || !canSelect || !!query.error}
            onChange={event => setSelected(current => event.target.checked ? [...current, hunk.id] : current.filter(id => id !== hunk.id))} />采用修改 {index + 1}</label>
          <strong>原文</strong><p>{hunk.old_text || '（新增位置）'}</p>
          <strong>优化稿</strong><p>{hunk.new_text || '（删除此处）'}</p>
        </article>)}
        <button type="button" disabled={disabled || !canSelect || !!query.error || !ids.length}
          onClick={() => void onAccept(ids)}>采纳选中修改</button>
      </>}
    </>}
  </section>;
}
