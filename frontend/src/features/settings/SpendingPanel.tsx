import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, apiError, type ModelConfig } from '../../lib/api';
import { DiagnosticError } from '../../components/DiagnosticError';
import '../../styles/product-tools.css';

type Price = { base_url: string; model: string; input_per_million: string; output_per_million: string };
type Settings = { revision: number; global_limit: string | null; project_limits: Record<string, string>; prices: Price[] };
type Entry = { id: string; created_at: string; project_id: string | null; model: string; task: string; stage: string | null;
  status: string; cost_cny: string | null; reserve_cny: string | null; usage_source: string | null; can_reconcile: boolean };
type Report = { settings: Settings; totals: { settled_cny: string; reserved_cny: string; committed_cny: string;
  unresolved_count: number; unpriced_count: number; estimated_count: number }; entries: Entry[] };
async function request<T>(suffix = '', init?: RequestInit): Promise<T> {
  const result = await fetch(`/api/v1/settings/spending${suffix}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  if (!result.ok) throw await apiError(result);
  return result.json() as Promise<T>;
}
const money = (value: string | null) => value === null ? '未定价' : `¥${value}`;
const states: Record<string, string> = { reserved: '已预留', sent: '已发送，待核对', unknown: '结果未知', settled: '已记账', not_sent: '未发送，不计费', reconciled: '已人工核对' };

export function SpendingPanel({ projectId }: { projectId: string }) {
  const cache = useQueryClient();
  const [scope, setScope] = useState<'all' | 'project'>('project');
  const [error, setError] = useState<unknown>();
  const [reconciling, setReconciling] = useState<Entry>();
  const [amount, setAmount] = useState(''), [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const query = useQuery({ queryKey: ['spending', projectId, scope], queryFn: async () => {
    const value = await request<Report>(scope === 'project' ? `?project_id=${encodeURIComponent(projectId)}` : '');
    if (!value?.settings || !value.totals || !Array.isArray(value.entries)) throw Error('费用记录格式无效，请刷新重试。');
    return value;
  } });
  const model = useQuery({ queryKey: ['model-settings'], queryFn: api.modelSettings });
  return <section className="product-tool spending-panel" aria-labelledby="spending-heading">
    <header><h2 id="spending-heading">费用与预算</h2><p>按配置单价记录远端文本调用，包含写作、优化、上下文压缩、导入分析与重试。演示和本地模型不计 API 费用。</p></header>
    <p className="subtle">这是本地估算账本，不是服务商余额。单价以人民币 / 百万 token 填写；缓存折扣、思考 token 或服务商特殊计价可能产生差异。限额采用累计支出加请求预留金额，在发送前检查。</p>
    {query.error && <DiagnosticError error={query.error} />}
    {!query.data && query.isPending && <p role="status">正在读取费用记录…</p>}
    <div className="product-actions"><label>查看范围<select value={scope} onChange={e => setScope(e.target.value as typeof scope)}><option value="project">当前小说</option><option value="all">所有小说与连接测试</option></select></label>
      <button disabled={query.isFetching} onClick={() => void query.refetch()}>刷新费用</button></div>
    {query.data && <>
      <dl className="spending-totals"><div><dt>已记账</dt><dd>{money(query.data.totals.settled_cny)}</dd></div><div><dt>待核对预留</dt><dd>{money(query.data.totals.reserved_cny)}</dd></div><div><dt>累计占用</dt><dd>{money(query.data.totals.committed_cny)}</dd></div></dl>
      {(query.data.totals.unpriced_count > 0 || query.data.totals.estimated_count > 0 || query.data.totals.unresolved_count > 0) && <p role="status">未定价 {query.data.totals.unpriced_count} 项，估算用量 {query.data.totals.estimated_count} 项，待核对 {query.data.totals.unresolved_count} 项。未知请求保留额度；核对服务商账单后可填写实际金额。</p>}
      {model.data && <SpendingForm key={`${model.data.base_url}:${model.data.model}`} value={query.data.settings} model={model.data} projectId={projectId}
        onSave={async value => { const saved = await request<Settings>('', { method: 'PUT', body: JSON.stringify(value) }); await cache.invalidateQueries({ queryKey: ['spending'] }); return saved; }} />}
      {model.error && <DiagnosticError error={model.error} message="模型配置读取失败，暂不能设置当前模型单价。" />}
      <details><summary>发送记录（优先待核对，最多 100 条）</summary><p>先显示未核对与未定价记录，再补充最近已记账记录。核对后刷新可继续处理更早记录；累计金额包含全部历史。</p><div className="product-table-scroll"><table><thead><tr><th>时间 / 任务</th><th>模型</th><th>状态</th><th>金额</th><th>操作</th></tr></thead><tbody>
        {query.data.entries.map(entry => <tr key={entry.id}><td>{new Date(entry.created_at).toLocaleString()}<small>{entry.stage || entry.task}</small></td><td>{entry.model}</td><td>{states[entry.status] ?? entry.status}{entry.usage_source && ['estimated', 'mixed'].includes(entry.usage_source) ? ' · 估算' : ''}</td><td>{money(entry.cost_cny ?? entry.reserve_cny)}</td><td>{entry.can_reconcile && <button onClick={() => { setReconciling(entry); setAmount(''); setNote(''); }}>核对金额</button>}</td></tr>)}
        {!query.data.entries.length && <tr><td colSpan={5}>暂无付费调用记录。历史版本升级前的请求不会回填为零元。</td></tr>}
      </tbody></table></div></details>
    </>}
    {reconciling && <form className="product-form" onSubmit={e => { e.preventDefault(); setBusy(true); setError(undefined); void request(`/entries/${encodeURIComponent(reconciling.id)}/reconcile`, { method: 'POST', body: JSON.stringify({ actual_cny: amount, note }) })
      .then(async () => { setReconciling(undefined); await cache.invalidateQueries({ queryKey: ['spending'] }); }).catch(setError).finally(() => setBusy(false)); }}>
      <h3>核对 {reconciling.model} 的请求金额</h3><p>请以服务商账单为准。确认没有扣费可填 0；此操作会保留人工核对记录。</p>
      <fieldset disabled={busy}><label>实际人民币金额<input required type="number" min="0" max="1000000" step="0.000001" value={amount} onChange={e => setAmount(e.target.value)} /></label><label>核对依据（不要填写密钥或正文）<input required maxLength={200} value={note} onChange={e => setNote(e.target.value)} /></label>
      <div className="product-actions"><button className="primary-action" type="submit">{busy ? '正在保存…' : '确认实际金额'}</button><button type="button" onClick={() => setReconciling(undefined)}>取消</button></div></fieldset>
    </form>}
    {!!error && <DiagnosticError error={error} />}
  </section>;
}

function SpendingForm({ value, model, projectId, onSave }: { value: Settings; model: ModelConfig; projectId: string; onSave: (value: Settings) => Promise<Settings> }) {
  const price = value.prices.find(p => p.base_url === model.base_url && p.model === model.model);
  const [baseline, setBaseline] = useState(value);
  const [global, setGlobal] = useState(value.global_limit ?? ''), [project, setProject] = useState(value.project_limits[projectId] ?? '');
  const [input, setInput] = useState(price?.input_per_million ?? ''), [output, setOutput] = useState(price?.output_per_million ?? '');
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [saved, setSaved] = useState(false);
  return <form className="product-form" onSubmit={e => { e.preventDefault(); setBusy(true); setError(undefined); setSaved(false);
    const limits = { ...baseline.project_limits }; if (project === '') delete limits[projectId]; else limits[projectId] = project;
    const prices = baseline.prices.filter(p => p.base_url !== model.base_url || p.model !== model.model);
    if (model.mode === 'api' && input !== '' && output !== '') prices.push({ base_url: model.base_url, model: model.model, input_per_million: input, output_per_million: output });
    void onSave({ ...baseline, global_limit: global === '' ? null : global, project_limits: limits, prices: model.mode === 'api' ? prices : baseline.prices })
      .then(next => { setBaseline(next); setSaved(true); }).catch(setError).finally(() => setBusy(false)); }}>
    <fieldset disabled={busy}><legend>累计预算与当前模型单价</legend><p>预算留空表示不限制，填 0 表示暂停付费调用。更换模型后请为新模型设置单价。</p>
      <div className="product-form-grid"><label>全局累计预算（元）<input type="number" min="0" max="1000000" step="0.000001" value={global} onChange={e => { setGlobal(e.target.value); setSaved(false); }} /></label><label>本小说累计预算（元）<input type="number" min="0" max="1000000" step="0.000001" value={project} onChange={e => { setProject(e.target.value); setSaved(false); }} /></label>
        {model.mode === 'api' && <><label>{model.model} · 输入单价<input required={output !== '' || global !== '' || project !== ''} type="number" min="0" max="1000000" step="0.000001" value={input} onChange={e => { setInput(e.target.value); setSaved(false); }} /></label><label>{model.model} · 输出单价<input required={input !== '' || global !== '' || project !== ''} type="number" min="0" max="1000000" step="0.000001" value={output} onChange={e => { setOutput(e.target.value); setSaved(false); }} /></label></>}
      </div><button className="primary-action" type="submit">{busy ? '正在保存…' : '保存费用设置'}</button>
      {baseline.revision !== value.revision && <button type="button" onClick={() => { setBaseline(value); setGlobal(value.global_limit ?? ''); setProject(value.project_limits[projectId] ?? ''); setInput(price?.input_per_million ?? ''); setOutput(price?.output_per_million ?? ''); setError(undefined); setSaved(false); }}>放弃当前输入并载入最新费用设置</button>}
    </fieldset>{saved && <p role="status">费用设置已保存，下次发送立即生效。</p>}{!!error && <DiagnosticError error={error} />}
  </form>;
}
