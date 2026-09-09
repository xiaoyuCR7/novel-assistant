import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MaterialEditorSheet } from '../src/features/library/MaterialEditorSheet';
import { StyleLab } from '../src/features/style/StyleLab';
import { ModelSettings, ModelSettingsForm } from '../src/features/settings/ModelSettings';
import { api, type ModelConfig } from '../src/lib/api';

afterEach(() => vi.restoreAllMocks());
const material = { id: 's1', type: 'style', title: 'Cold', content: '{"rhythm":"crisp"}',
  preview: '', revision: 3, is_pinned: false, deleted_at: null, purge_after: null,
  record: { config: { rhythm: 'crisp' }, is_active: true } };

it('edits typed style config without sending competing content, and retains input on failure', async () => {
  const save = vi.fn().mockRejectedValue(new Error('revision changed'));
  const close = vi.fn();
  render(<MaterialEditorSheet item={material} onSave={save} onClose={close} onDelete={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('类型字段 JSON'), { target: { value: '{"config":{"rhythm":"lyrical"}}' } });
  fireEvent.click(screen.getByLabelText('启用风格方案'));
  fireEvent.click(screen.getByRole('button', { name: '保存素材' }));
  await screen.findByText('revision changed');
  expect(save).toHaveBeenCalledWith('style', { title: 'Cold', revision: 3, is_pinned: false,
    fields: { config: { rhythm: 'lyrical' }, is_active: false } });
  expect(screen.getByLabelText('类型字段 JSON')).toHaveValue('{"config":{"rhythm":"lyrical"}}');
  expect(close).not.toHaveBeenCalled();
});

it('keeps style controls disabled during mutation and displays rule failures', async () => {
  let reject!: (error: Error) => void;
  const toggle = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<StyleLab profiles={[{ id: 's1', name: 'Cold', revision: 3, is_active: true, config: {} }]}
    candidates={[{ id: 'r1', instruction: 'short', category: 'rhythm', status: 'candidate' }]} rules={[]}
    onUpdateProfile={toggle} onConfirm={async () => { throw new Error('rule failed'); }} onDisable={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: '停用方案 Cold' }));
  expect(screen.getByRole('button', { name: '停用方案 Cold' })).toBeDisabled();
  await act(async () => reject(new Error('profile failed')));
  expect(await screen.findByText('profile failed')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '确认风格规则' }));
  expect(await screen.findByText('rule failed')).toBeVisible();
});

it('saves model generation limits and distinguishes them from retrieval input budget', async () => {
  const value: ModelConfig = { mode: 'demo', base_url: '', model: '', has_api_key: false,
    external_consent: false, output_token_budget: 8192, context_capacity: 65536,
    deadline_seconds: 240, output_parameter: 'max_completion_tokens', thinking_mode: 'provider_default' };
  const save = vi.fn().mockResolvedValue({ ...value, deadline_seconds: 300 });
  render(<ModelSettingsForm value={value} onSave={save} onTest={vi.fn()} />);
  expect(screen.getByLabelText('最大输出 Token')).toHaveValue(8192);
  expect(screen.getByLabelText('模型上下文容量')).toHaveAttribute('max', '1048576');
  fireEvent.change(screen.getByLabelText('请求超时秒数'), { target: { value: '300' } });
  fireEvent.change(screen.getByLabelText('思考模式'), { target: { value: 'disabled' } });
  expect(screen.getByRole('button', { name: '测试连接' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({
    output_token_budget: 8192, context_capacity: 65536, deadline_seconds: 300,
    output_parameter: 'max_completion_tokens', thinking_mode: 'disabled' })));
  expect(screen.getByText(/输入资料预算/)).toBeInTheDocument();
});

it.each(['https://example.com/v1/', 'https://example.com/v1/chat/completions'])('adopts saved canonical model settings without remounting the form (%s)', async (baseUrl) => {
  const original: ModelConfig = { mode: 'api', base_url: 'https://example.com/v1', model: 'old-model',
    has_api_key: true, external_consent: true, output_token_budget: 8192, context_capacity: 65536,
    deadline_seconds: 240, output_parameter: 'max_completion_tokens' };
  const saved: ModelConfig = { ...original, model: 'new-model' };
  vi.spyOn(api, 'modelSettings').mockResolvedValue(original);
  let finishSave!: (config: ModelConfig) => void;
  const save = vi.spyOn(api, 'saveModelSettings').mockImplementation(() => new Promise(resolve => { finishSave = resolve; }));
  const test = vi.spyOn(api, 'testModel').mockResolvedValue({ ok: true });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ModelSettings /></QueryClientProvider>);
  const url = await screen.findByLabelText('API 地址');
  const model = screen.getByLabelText('模型名称');
  fireEvent.change(url, { target: { value: baseUrl } });
  fireEvent.change(model, { target: { value: ' new-model ' } });
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
  expect(url).toBeDisabled();
  expect(model).toBeDisabled();
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ base_url: baseUrl, model: ' new-model ' }));
  await act(async () => finishSave(saved));
  expect(screen.getByLabelText('API 地址')).toBe(url);
  expect(screen.getByLabelText('模型名称')).toBe(model);
  expect(url).toHaveValue('https://example.com/v1');
  expect(model).toHaveValue('new-model');
  expect(screen.getByRole('button', { name: '测试连接' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
  expect(test).toHaveBeenCalledTimes(1);
  expect(await screen.findByText('连接成功，可以开始创作。')).toBeVisible();
});
