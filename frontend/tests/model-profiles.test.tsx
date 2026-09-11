import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { ModelProfiles } from '../src/features/settings/ModelProfiles';
import { ModelSettings, ModelSettingsForm } from '../src/features/settings/ModelSettings';
import type { ModelConfig } from '../src/lib/api';

const current = { mode: 'demo', base_url: '', model: '', external_consent: false, has_api_key: false,
  context_capacity: 32768, output_token_budget: 4096, deadline_seconds: 180,
  output_parameter: 'max_tokens', thinking_mode: 'provider_default', config_revision: 'a'.repeat(64) } as ModelConfig & { config_revision: string };
const target = { ...current, mode: 'local', base_url: 'http://127.0.0.1:11434', model: 'local-story', config_revision: 'b'.repeat(64) };
const profile = { id: 'profile-1', name: '本地创作', revision: 1, config: target, is_current: false };
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const caches: QueryClient[] = [];
afterEach(() => { caches.forEach(c => c.clear()); caches.length = 0; vi.unstubAllGlobals(); localStorage.clear(); });
function setup(handler?: (url: URL, init?: RequestInit) => Response | Promise<Response> | undefined,
               integrated = false) {
  const requests: { url: URL; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local'); requests.push({ url, init });
    const result = handler?.(url, init); if (result !== undefined) return result;
    if (url.pathname.endsWith('/model/profiles') && !init?.method) return response({ items: [profile], current_config_revision: current.config_revision });
    if (url.pathname.endsWith('/settings/model') && !init?.method) return response(current);
    throw Error(`Unexpected ${url} ${init?.method}`);
  }));
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } }); caches.push(cache);
  const applied = vi.fn();
  const tree = () => <QueryClientProvider client={cache}>{integrated ? <ModelSettings /> : <ModelProfiles value={current} onApplied={applied} />}</QueryClientProvider>;
  const app = render(tree());
  return { requests, applied, cache, remount: () => { app.unmount(); return render(tree()); } };
}

it('lists saved configurations without creating a provider or sending a connection test', async () => {
  const app = setup();
  expect(await screen.findByText('本地创作')).toBeVisible();
  expect(screen.getByText('local-story')).toBeVisible();
  expect(app.requests.every(r => !r.init?.method)).toBe(true);
});

it('captures only saved settings and retries the same create identity after uncertain response', async () => {
  const bodies: Record<string, unknown>[] = [];
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/model/profiles') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)); bodies.push(body);
      return bodies.length === 1 ? Promise.reject(Error('暂时断线')) : response({ ...profile, id: body.id, name: body.name });
    }
  });
  await screen.findByText('本地创作');
  await userEvent.click(screen.getByRole('button', { name: '保存为命名方案' }));
  fireEvent.change(screen.getByLabelText('方案名称'), { target: { value: '快速讨论' } });
  await userEvent.click(screen.getByRole('button', { name: '保存方案' }));
  await screen.findByText('暂时断线');
  app.remount();
  await userEvent.click(await screen.findByRole('button', { name: '核对并重试原操作' }));
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1]).toEqual(bodies[0]);
  expect(bodies[0]).toMatchObject({ name: '快速讨论', expected_config_revision: current.config_revision });
  expect(JSON.stringify(bodies)).not.toContain('api_key');
});

it('explicitly applies a profile with both revisions and refreshes current model cache', async () => {
  const app = setup((url, init) => url.pathname.endsWith('/apply') && init?.method === 'POST' ? response(target) : undefined);
  await userEvent.click(await screen.findByRole('button', { name: '应用方案 本地创作' }));
  await waitFor(() => expect(app.applied).toHaveBeenCalledWith(target));
  expect(app.cache.getQueryData(['model-settings'])).toEqual(target);
  const posted = app.requests.find(r => r.init?.method === 'POST');
  expect(JSON.parse(String(posted?.init?.body))).toEqual({ expected_revision: 1, expected_config_revision: current.config_revision });
  expect(app.requests.some(r => r.url.pathname.endsWith('/test'))).toBe(false);
});

it('preserves unsaved model form input and hydrates the form only after an explicit discard and apply', async () => {
  const app = setup((url, init) => url.pathname.endsWith('/apply') && init?.method === 'POST' ? response(target) : undefined, true);
  await screen.findByText('本地创作');
  await userEvent.click(screen.getByText('生成限制与兼容性'));
  fireEvent.change(screen.getByLabelText('模型上下文容量'), { target: { value: '65536' } });
  await userEvent.click(screen.getByRole('button', { name: '应用方案 本地创作' }));
  await screen.findByText('请先保存或放弃未保存的模型设置，再操作配置方案。');
  expect(screen.getByLabelText('模型上下文容量')).toHaveValue(65536);
  expect(app.requests.filter(r => r.init?.method === 'POST')).toHaveLength(0);
  await userEvent.click(screen.getByRole('button', { name: '放弃未保存的模型设置' }));
  await userEvent.click(screen.getByRole('button', { name: '应用方案 本地创作' }));
  await waitFor(() => expect(screen.getByLabelText('运行模式')).toHaveValue('local'));
  expect(screen.getByLabelText('模型名称')).toHaveValue('local-story');
});

it('requires a deliberate delete and uses the displayed profile revision', async () => {
  const app = setup((url, init) => init?.method === 'DELETE' ? response({ deleted: true, id: profile.id }) : undefined);
  await userEvent.click(await screen.findByRole('button', { name: '删除方案 本地创作' }));
  expect(app.requests.every(r => !r.init?.method)).toBe(true);
  await userEvent.click(within(screen.getByRole('group', { name: '删除模型方案' })).getByRole('button', { name: '确认删除方案' }));
  await waitFor(() => expect(app.requests.some(r => r.init?.method === 'DELETE')).toBe(true));
  expect(app.requests.find(r => r.init?.method === 'DELETE')?.url.searchParams.get('expected_revision')).toBe('1');
});

it('retains the original edit revision when another settings update reaches the form', async () => {
  const save = vi.fn().mockResolvedValue(current);
  const app = render(<ModelSettingsForm value={current} onSave={save} onTest={vi.fn()} />);
  await userEvent.click(screen.getByText('生成限制与兼容性'));
  fireEvent.change(screen.getByLabelText('模型上下文容量'), { target: { value: '65536' } });
  app.rerender(<ModelSettingsForm value={{ ...current, config_revision: 'c'.repeat(64) } as typeof current} onSave={save} onTest={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: '保存设置' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ expected_config_revision: current.config_revision, context_capacity: 65536 }));
});

it('keeps configuration unchanged after an application conflict and shows diagnostics', async () => {
  const app = setup((url, init) => url.pathname.endsWith('/apply') && init?.method === 'POST' ? response({ detail: {
    code: 'MODEL_CONFIG_CHANGED', message: '当前设置已改变，请刷新。', diagnostic_id: 'profile-conflict',
  } }, 409) : undefined);
  app.cache.setQueryData(['model-settings'], current);
  await userEvent.click(await screen.findByRole('button', { name: '应用方案 本地创作' }));
  await screen.findByText('当前设置已改变，请刷新。');
  expect(screen.getByText('诊断编号：profile-conflict')).toBeVisible();
  expect(app.cache.getQueryData(['model-settings'])).toEqual(current);
  expect(app.applied).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: '核对并重试原操作' })).not.toBeInTheDocument();
});
