import { useEffect, useRef, useState } from 'react';
import { previewBackup, restoreBackup, type BackupPreview } from '../../lib/projectTransfers';
import type { Project } from '../../lib/types';
import { DiagnosticError } from '../../components/DiagnosticError';
import './project-transfers.css';

interface Props {
  onRestored: (project: Project) => void | Promise<void>;
  onCancel?: () => void;
  onBusyChange?: (busy: boolean) => void;
}

export function BackupRestore({ onRestored, onCancel, onBusyChange }: Props) {
  const [file, setFile] = useState<File>();
  const [preview, setPreview] = useState<BackupPreview>();
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<'preview' | 'restore' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const mounted = useRef(true), pending = useRef(false), previewRequest = useRef<AbortController | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; previewRequest.current?.abort(); }; }, []);

  async function check() {
    if (!file || pending.current) return;
    pending.current = true; setBusy('preview'); onBusyChange?.(true); setError(null); setPreview(undefined); setConfirmed(false);
    const controller = new AbortController(); previewRequest.current = controller;
    try { const result = await previewBackup(file, controller.signal); if (mounted.current) setPreview(result); }
    catch (reason) { if (mounted.current) setError(reason); }
    finally { pending.current = false; onBusyChange?.(false); if (mounted.current) setBusy(null); }
  }
  async function restore() {
    if (!file || !preview || preview.conflict || !confirmed || pending.current || done) return;
    pending.current = true; setBusy('restore'); onBusyChange?.(true); setError(null);
    try {
      const project = await restoreBackup(file, preview.archive_hash);
      if (mounted.current) { setDone(true); await onRestored(project); }
    } catch (reason) {
      if (mounted.current) setError(reason);
    } finally { pending.current = false; onBusyChange?.(false); if (mounted.current) setBusy(null); }
  }

  return <section className="project-transfer" aria-label="恢复项目备份" aria-busy={Boolean(busy)}>
    <header><h2>恢复项目备份</h2><p>导入完整 ZIP，保留正文、素材、版本与会话历史。</p></header>
    <label className="transfer-field">项目备份 ZIP
      <input type="file" accept=".zip,application/zip" disabled={Boolean(busy) || done} onChange={event => {
        const next = event.target.files?.[0];
        setPreview(undefined); setConfirmed(false); setError('');
        if (next && next.size > 200 * 1024 * 1024) { setFile(undefined); setError('备份 ZIP 超过 200 MiB 上限。'); }
        else setFile(next);
      }} />
    </label>
    <button type="button" disabled={!file || Boolean(busy) || done} onClick={() => void check()}>
      {busy === 'preview' ? '正在检查…' : '检查备份'}
    </button>
    {preview && <div className="transfer-preview">
      <h3>{preview.title}</h3>
      <p>{preview.verified ? '文件完整性校验通过' : '旧版备份：无逐文件校验清单'}</p>
      <dl><div><dt>章节</dt><dd>{preview.counts.chapters}</dd></div><div><dt>版本</dt><dd>{preview.counts.versions}</dd></div>
        <div><dt>任务记录</dt><dd>{preview.counts.jobs}</dd></div><div><dt>素材文件</dt><dd>{preview.counts.assets}</dd></div></dl>
      {preview.warnings.map(warning => <p key={warning}>{warning}</p>)}
      {preview.conflict ? <p role="status">同一项目已存在，无法覆盖。请在空白工作区中恢复这份备份。</p> :
        <label className="transfer-choice"><input type="checkbox" checked={confirmed} disabled={Boolean(busy) || done}
          onChange={event => setConfirmed(event.target.checked)} />
          <span>我已核对备份。恢复会保留原项目身份，未完成的 AI 任务需手动继续。</span></label>}
    </div>}
    {error != null && error !== '' && <DiagnosticError error={error} />}
    {done && <p role="status">项目已恢复，历史内容已保留。</p>}
    <footer>{onCancel && <button type="button" disabled={Boolean(busy)} onClick={onCancel}>关闭</button>}
      <button type="button" className="primary" disabled={!preview || preview.conflict || !confirmed || Boolean(busy) || done}
        onClick={() => void restore()}>{busy === 'restore' ? '正在恢复…' : '恢复项目'}</button></footer>
  </section>;
}
