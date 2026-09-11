import { useEffect, useRef, useState } from 'react';
import { exportManuscript, type ManuscriptCommand } from '../../lib/projectTransfers';
import type { StoryNode } from '../../lib/types';
import { DiagnosticError } from '../../components/DiagnosticError';
import './project-transfers.css';

interface Props {
  projectId: string;
  nodes: Pick<StoryNode, 'id' | 'title' | 'kind' | 'parent_id'>[];
  onClose?: () => void;
}

export function ManuscriptExport(props: Props) { return <ExportForm key={props.projectId} {...props} />; }

function ExportForm({ projectId, nodes, onClose }: Props) {
  const [format, setFormat] = useState<ManuscriptCommand['format']>('txt');
  const [source, setSource] = useState<ManuscriptCommand['source']>('published');
  const [order, setOrder] = useState<ManuscriptCommand['order']>('story');
  const [all, setAll] = useState(true), [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null), [done, setDone] = useState(false);
  const pending = useRef(false), request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const choices = nodes.filter(node => node.kind !== 'scene');

  async function download() {
    if (pending.current || (!all && !selected.length)) return;
    pending.current = true; setBusy(true); setError(''); setDone(false);
    const controller = new AbortController(); request.current = controller;
    try {
      const blob = await exportManuscript(projectId, { format, source, order, node_ids: all ? [] : selected }, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = `manuscript-${projectId}.${format === 'markdown' ? 'md' : 'txt'}`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000); setDone(true);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason);
    } finally { pending.current = false; if (!controller.signal.aborted) setBusy(false); }
  }

  return <section className="project-transfer" aria-label="导出正文" aria-busy={busy}>
    <header><h2>导出正文</h2><p>只包含小说标题、章节标题和已保存的正文。</p></header>
    <fieldset disabled={busy} className="transfer-options"><legend className="sr-only">导出设置</legend>
      <label className="transfer-field">文件格式<select value={format} onChange={event => setFormat(event.target.value as ManuscriptCommand['format'])}>
        <option value="txt">纯文本 TXT</option><option value="markdown">Markdown</option></select></label>
      <label className="transfer-field">正文来源<select value={source} onChange={event => setSource(event.target.value as ManuscriptCommand['source'])}>
        <option value="published">当前正式版本</option><option value="working">已保存的工作副本</option></select></label>
      <p>{source === 'published' ? '使用各章当前正式版本；没有正式版本的章节会提示补齐。' : '使用最近保存的工作副本；请先保存编辑器中的修改。'}未采纳的 AI 候选不会进入导出文件。</p>
      <fieldset className="transfer-scope"><legend>导出范围</legend>
        <label className="transfer-choice"><input type="radio" name="export-scope" checked={all} onChange={() => setAll(true)} />整部小说</label>
        <label className="transfer-choice"><input type="radio" name="export-scope" checked={!all} onChange={() => setAll(false)} />选择卷章</label>
      </fieldset>
      {!all && <div className="transfer-nodes" role="group" aria-label="选择要导出的卷章">
        {choices.map(node => <label key={node.id} className="transfer-choice">
          <input type="checkbox" checked={selected.includes(node.id)} onChange={event => setSelected(current => event.target.checked
            ? [...current, node.id] : current.filter(id => id !== node.id))} />
          <span>{node.title}<small>{node.kind === 'volume' ? '整卷' : '章节'}</small></span></label>)}
      </div>}
      <label className="transfer-field">导出顺序<select value={order} onChange={event => setOrder(event.target.value as ManuscriptCommand['order'])}>
        <option value="story">目录顺序</option><option value="selection" disabled={all}>勾选顺序</option></select></label>
      {!all && selected.length > 0 && order === 'selection' && <p>顺序：{selected.map(id => choices.find(node => node.id === id)?.title ?? '已移除章节').join(' → ')}。整卷按目录展开，重复章节只导出一次。</p>}
    </fieldset>
    {error != null && error !== '' && <DiagnosticError error={error} />}
    {done && <p role="status">正文文件已开始下载。</p>}
    <footer>{onClose && <button type="button" onClick={onClose}>关闭</button>}
      <button type="button" className="primary" disabled={busy || !choices.some(node => node.kind === 'chapter') || (!all && !selected.length)}
        onClick={() => void download()}>{busy ? '正在导出…' : '下载正文'}</button></footer>
  </section>;
}
