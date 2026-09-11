import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { apiError } from '../../lib/api';
import { DiagnosticError } from '../../components/DiagnosticError';
import '../../styles/product-tools.css';

type Policy = { enabled: boolean; interval_hours: number; retention_count: number; revision: number };
type Report = { settings: Policy; last_checked_at: string | null; last_success_at: string | null; last_error: string | null;
  backups: { id: string; created_at: string; size_bytes: number }[]; result?: string };
const date = (value: string | null) => value ? new Date(value).toLocaleString() : '尚无记录';
export function AutomaticBackups({ projectId }: { projectId: string }) {
  const cache = useQueryClient(), key = ['automatic-backups', projectId];
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}/automatic-backups`;
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [notice, setNotice] = useState('');
  async function request(suffix = '', init?: RequestInit): Promise<Report> {
    const response = await fetch(base + suffix, { ...init, headers: { 'Content-Type': 'application/json' } });
    if (!response.ok) throw await apiError(response);
    const report = await response.json();
    if (!report?.settings || !Array.isArray(report.backups)) throw Error('备份记录格式无效，请重试。');
    return report;
  }
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => request('', { signal }) });
  return <section className="product-tool" aria-label="自动备份">
    <header><h2>自动备份</h2><p>为当前小说保留可恢复的完整项目副本。只有后台服务运行时才按周期检查；内容未变化时跳过新副本。</p></header>
    <p className="subtle">备份保存在本机项目目录，不能防止磁盘损坏。请定期下载重要副本到其他设备；本机未保存草稿与全局模型密钥不包含在项目 ZIP 中。</p>
    {query.isPending && <p role="status">正在读取备份记录…</p>}
    {!!query.error && <DiagnosticError error={query.error} />}
    <div className="product-actions"><button disabled={busy} onClick={() => { setBusy(true); setError(undefined); setNotice('');
      void request('/run', { method: 'POST' }).then(async result => {
        await cache.cancelQueries({ queryKey: key, exact: true });
        cache.setQueryData(key, result);
        setNotice(result.result === 'unchanged' ? '项目内容未变化，已保留最近的有效备份。' : '备份已创建并通过完整性校验。'); })
        .catch(setError).finally(() => setBusy(false)); }}>{busy ? '正在处理…' : '立即备份'}</button>
      <button disabled={busy || query.isFetching} onClick={() => void query.refetch()}>刷新备份记录</button></div>
    {query.data && <>
      <dl className="spending-totals"><div><dt>最近成功备份</dt><dd className="backup-date">{date(query.data.last_success_at)}</dd></div>
        <div><dt>最近检查</dt><dd className="backup-date">{date(query.data.last_checked_at)}</dd></div>
        <div><dt>保留副本</dt><dd>{query.data.backups.length} 份</dd></div></dl>
      {query.data.last_error && <p role="status" className="error-note">{query.data.last_error}</p>}
      <PolicyForm value={query.data.settings} disabled={busy} onSave={async policy => {
        setBusy(true);
        try {
          const report = await request('', { method: 'PUT', body: JSON.stringify(policy) });
          await cache.cancelQueries({ queryKey: key, exact: true });
          cache.setQueryData(key, report);
          return report.settings;
        } finally { setBusy(false); }
      }} />
      <details><summary>查看与下载备份</summary><ul className="backup-history">
        {query.data.backups.map(item => <li key={item.id}><span>{date(item.created_at)} · {(item.size_bytes / 1024).toFixed(1)} KB</span>
          <a href={`${base}/${encodeURIComponent(item.id)}`} download>下载 ZIP</a></li>)}
        {!query.data.backups.length && <li>尚无自动备份，可先点击“立即备份”。</li>}
      </ul></details>
    </>}
    {notice && <p role="status">{notice}</p>}{!!error && <DiagnosticError error={error} />}
  </section>;
}

function PolicyForm({ value, disabled, onSave }: { value: Policy; disabled: boolean; onSave: (value: Policy) => Promise<Policy> }) {
  const [draft, setDraft] = useState(value), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [saved, setSaved] = useState(false);
  return <form className="product-form" onSubmit={event => { event.preventDefault(); setBusy(true); setError(undefined); setSaved(false);
    void onSave(draft).then(result => { setDraft(result); setSaved(true); }).catch(setError).finally(() => setBusy(false)); }}>
    <fieldset disabled={busy || disabled}><legend>定期备份设置</legend>
      <label className="backup-toggle"><input type="checkbox" checked={draft.enabled} onChange={e => { setDraft({ ...draft, enabled: e.target.checked }); setSaved(false); }} />启用定期备份</label>
      <div className="product-form-grid"><label>检查间隔（小时）<input type="number" min="1" max="168" required value={draft.interval_hours} onChange={e => { setDraft({ ...draft, interval_hours: e.target.valueAsNumber }); setSaved(false); }} /></label>
        <label>保留备份数量<input type="number" min="1" max="30" required value={draft.retention_count} onChange={e => { setDraft({ ...draft, retention_count: e.target.valueAsNumber }); setSaved(false); }} /></label></div>
      <p className="subtle">新副本验证成功后才清理本功能创建的超额旧副本；手动下载和其他文件不受影响。</p>
      <button className="primary-action" type="submit">{busy ? '正在保存…' : '保存备份设置'}</button>
      {value.revision !== draft.revision && <button type="button" onClick={() => { setDraft(value); setError(undefined); setSaved(false); }}>放弃输入并载入最新备份设置</button>}
    </fieldset>{saved && <p role="status">备份设置已保存。</p>}{!!error && <DiagnosticError error={error} />}
  </form>;
}
