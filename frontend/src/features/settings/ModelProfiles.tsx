import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, apiError, type ModelConfig } from '../../lib/api';
import { DiagnosticError } from '../../components/DiagnosticError';
import '../../styles/model-profiles.css';

type RevisionedConfig = ModelConfig & { config_revision: string };
type Profile = { id: string; name: string; revision: number; config: RevisionedConfig; is_current: boolean };
type ProfilePage = { items: Profile[]; current_config_revision: string };
type Pending = { kind: 'create'; body: { id: string; name: string; expected_config_revision: string } }
  | { kind: 'rename'; id: string; body: { name: string; expected_revision: number } }
  | { kind: 'delete'; id: string; revision: number }
  | { kind: 'apply'; id: string; body: { expected_revision: number; expected_config_revision: string } };
const base = '/api/v1/settings/model/profiles';
const pendingKey = 'novel:model-profile-operation:v1';
const revision = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
export function modelConfigRevision(value: ModelConfig): string | undefined {
  const candidate = (value as Partial<RevisionedConfig>).config_revision;
  return revision(candidate) ? candidate : undefined;
}
const validConfig = (value: unknown): value is RevisionedConfig => !!value && typeof value === 'object'
  && ['demo', 'local', 'api'].includes((value as ModelConfig).mode)
  && typeof (value as ModelConfig).base_url === 'string' && typeof (value as ModelConfig).model === 'string'
  && typeof (value as ModelConfig).has_api_key === 'boolean' && revision((value as RevisionedConfig).config_revision);
const validProfile = (value: unknown): value is Profile => !!value && typeof value === 'object'
  && typeof (value as Profile).id === 'string' && !!(value as Profile).id
  && typeof (value as Profile).name === 'string' && !!(value as Profile).name.trim()
  && Number.isInteger((value as Profile).revision) && (value as Profile).revision >= 1
  && validConfig((value as Profile).config) && typeof (value as Profile).is_current === 'boolean';
async function request(url: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(url, init);
  if (!response.ok) throw await apiError(response);
  return response.json();
}
function readPending(): Pending | null {
  try {
    const value = JSON.parse(localStorage.getItem(pendingKey) ?? 'null') as Pending | null;
    if (!value || typeof value !== 'object') return null;
    if (value.kind === 'delete') return typeof value.id === 'string' && Number.isInteger(value.revision) ? value : null;
    if (!value.body || typeof value.body !== 'object') return null;
    if (value.kind === 'create') return typeof value.body.id === 'string' && typeof value.body.name === 'string'
      && revision(value.body.expected_config_revision) ? value : null;
    if (value.kind === 'rename') return typeof value.id === 'string' && typeof value.body.name === 'string'
      && Number.isInteger(value.body.expected_revision) ? value : null;
    if (value.kind === 'apply') return typeof value.id === 'string' && Number.isInteger(value.body.expected_revision)
      && revision(value.body.expected_config_revision) ? value : null;
  } catch { /* A damaged local receipt cannot authorize an operation. */ }
  return null;
}

export function ModelProfiles({ value, beforeApply, onApplied }: { value: ModelConfig;
  beforeApply?: () => boolean | Promise<boolean>; onApplied?: (value: RevisionedConfig) => void | Promise<void> }) {
  const cache = useQueryClient();
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState('');
  const [pending, setPending] = useState<Pending | null>(readPending);
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{ kind: 'create' } | { kind: 'rename' | 'delete'; profile: Profile }>();
  const [name, setName] = useState('');
  const locked = useRef(false), alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const query = useQuery({ queryKey: ['model-profiles'], retry: false,
    queryFn: async ({ signal }): Promise<ProfilePage> => {
      const page = await request(base, { signal }) as ProfilePage;
      if (!page || !Array.isArray(page.items) || !page.items.every(validProfile) || !revision(page.current_config_revision))
        throw Error('模型方案列表回执无效，请刷新后重试。');
      return page;
    } });
  const currentRevision = modelConfigRevision(value);
  const unavailable = busy || !!pending || query.isError || !query.data;
  function persist(operation: Pending | null) {
    setPending(operation);
    try { if (operation) localStorage.setItem(pendingKey, JSON.stringify(operation)); else localStorage.removeItem(pendingKey); }
    catch { /* A mounted retry still retains its original request identity. */ }
  }
  async function execute(operation: Pending) {
    if (locked.current) return;
    locked.current = true; setBusy(true); setError(undefined); setNotice('');
    try {
      if ((operation.kind === 'create' || operation.kind === 'apply') && beforeApply && !await beforeApply()) return;
      if (!alive.current) return;
      persist(operation);
      const url = operation.kind === 'create' ? base : `${base}/${encodeURIComponent(operation.id)}${operation.kind === 'apply' ? '/apply' : operation.kind === 'delete' ? `?expected_revision=${operation.revision}` : ''}`;
      const result = await request(url, { method: operation.kind === 'delete' ? 'DELETE' : operation.kind === 'rename' ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' }, ...(operation.kind === 'delete' ? {} : { body: JSON.stringify(operation.body) }) });
      if (operation.kind === 'delete') {
        if (!result || (result as { deleted?: unknown }).deleted !== true || (result as { id?: unknown }).id !== operation.id)
          throw Error('未收到有效删除回执，请核对并重试原操作。');
      } else if (operation.kind === 'apply') {
        if (!validConfig(result)) throw Error('未收到有效模型配置回执，请核对并重试原操作。');
        await cache.cancelQueries({ queryKey: ['model-settings'], exact: true });
        cache.setQueryData(['model-settings'], result);
      } else if (!validProfile(result) || result.id !== (operation.kind === 'create' ? operation.body.id : operation.id)) {
        throw Error('未收到有效方案回执，请核对并重试原操作。');
      }
      if (!alive.current) return;
      persist(null); setEditor(undefined);
      setNotice(operation.kind === 'apply' ? '方案已应用，新请求将使用此配置。未自动测试连接。'
        : operation.kind === 'delete' ? '方案已删除，当前模型配置保持不变。' : '模型方案已保存。');
      if (operation.kind === 'apply') {
        await onApplied?.(result as RevisionedConfig);
        void cache.invalidateQueries({ queryKey: ['health'] });
      }
      await cache.cancelQueries({ queryKey: ['model-profiles'], exact: true });
      await cache.invalidateQueries({ queryKey: ['model-profiles'] });
    } catch (caught) {
      if (!alive.current) return;
      if (caught instanceof ApiError && caught.status >= 400 && caught.status < 500) persist(null);
      setError(caught);
      void cache.invalidateQueries({ queryKey: ['model-profiles'] });
    } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  function open(next: NonNullable<typeof editor>) {
    setEditor(next); setName(next.kind === 'create' ? (value.model || '离线演示').slice(0, 100) : next.profile.name);
    setError(undefined); setNotice('');
  }
  return <section className="model-profiles" aria-labelledby="model-profiles-heading" aria-busy={busy}>
    <header><div><h2 id="model-profiles-heading">命名模型方案</h2><p>保存已落盘的连接设置，在常用模型之间切换。</p></div>
      <button type="button" disabled={unavailable || !currentRevision} onClick={() => open({ kind: 'create' })}>保存为命名方案</button></header>
    <p className="subtle">方案仅保存在本机，密钥保持系统加密。保存、改名和应用均不会调用模型；切换后，旧任务仍按原模型身份校验恢复。</p>
    {query.isPending && <p role="status">正在读取模型方案…</p>}
    {query.error && <DiagnosticError error={query.error} />}
    <button type="button" disabled={busy || query.isFetching} onClick={() => void query.refetch()}>刷新模型方案</button>
    {query.data && !query.data.items.length && <p className="subtle">还没有命名方案。先保存模型设置，再将它保存为方案。</p>}
    <ul className="model-profile-list">{query.data?.items.map(profile => <li key={profile.id}>
      <div><strong>{profile.name}</strong>{profile.is_current && <span className="model-profile-current">当前配置</span>}
        <p>{profile.config.model || '离线演示'}</p><small>{profile.config.mode === 'api' ? 'API 服务' : profile.config.mode === 'local' ? '本机模型' : '离线演示'} · 容量 {profile.config.context_capacity?.toLocaleString()} · 输出 {profile.config.output_token_budget?.toLocaleString()}{profile.config.has_api_key ? ' · 已保存加密密钥' : ''}</small></div>
      <div className="model-profile-actions"><button type="button" aria-label={`应用方案 ${profile.name}`} disabled={unavailable || !currentRevision || profile.is_current}
        onClick={() => void execute({ kind: 'apply', id: profile.id, body: { expected_revision: profile.revision, expected_config_revision: currentRevision! } })}>应用</button>
        <button type="button" aria-label={`重命名方案 ${profile.name}`} disabled={unavailable} onClick={() => open({ kind: 'rename', profile })}>改名</button>
        <button type="button" aria-label={`删除方案 ${profile.name}`} disabled={unavailable} onClick={() => open({ kind: 'delete', profile })}>删除</button></div>
    </li>)}</ul>
    {editor && editor.kind !== 'delete' && <form className="model-profile-editor" onSubmit={event => {
      event.preventDefault(); const title = name.trim(); if (!title || unavailable) return;
      if (editor.kind === 'create' && currentRevision) void execute({ kind: 'create', body: { id: crypto.randomUUID(), name: title, expected_config_revision: currentRevision } });
      else if (editor.kind === 'rename') void execute({ kind: 'rename', id: editor.profile.id, body: { name: title, expected_revision: editor.profile.revision } });
    }}><label>方案名称<input autoFocus maxLength={100} required value={name} onChange={event => setName(event.target.value)} disabled={busy || !!pending} /></label>
      {editor.kind === 'create' && <p>只保存已保存的模型设置，不包含上方尚未保存的输入。</p>}
      <div><button type="submit" disabled={unavailable || !name.trim()}>{busy ? '正在保存…' : '保存方案'}</button><button type="button" disabled={busy} onClick={() => setEditor(undefined)}>取消</button></div></form>}
    {editor?.kind === 'delete' && <div className="model-profile-editor" role="group" aria-label="删除模型方案"><p>删除“{editor.profile.name}”？只移除命名方案，不改变当前模型设置和已有任务。</p>
      <button type="button" disabled={unavailable} onClick={() => void execute({ kind: 'delete', id: editor.profile.id, revision: editor.profile.revision })}>确认删除方案</button>
      <button type="button" disabled={busy} onClick={() => setEditor(undefined)}>取消</button></div>}
    {!!error && <DiagnosticError error={error} />}
    {pending && <div className="model-profile-editor"><p>上次操作尚未收到确定回执。核对会继续使用原方案和版本。</p><button type="button" disabled={busy} onClick={() => void execute(pending)}>{busy ? '正在核对…' : '核对并重试原操作'}</button></div>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
