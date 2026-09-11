import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { ModelSettings } from '../src/features/settings/ModelSettings';
import { modelInputBudget } from '../src/features/ai/inputBudget';
import { api, ApiError, type ModelConfig } from '../src/lib/api';

const queryKey = ['model-settings'];
const original: ModelConfig = {
  mode: 'demo', base_url: '', model: '', has_api_key: false, external_consent: false,
  context_capacity: 32768, output_token_budget: 4096,
};
const clients: QueryClient[] = [];

afterEach(() => {
  clients.forEach(client => client.clear());
  clients.length = 0;
  vi.restoreAllMocks();
});

function BudgetConsumer() {
  const config = useQuery({ queryKey, queryFn: api.modelSettings });
  return <output aria-label="新任务默认输入预算">{config.error || !config.data ? '配置不可用' : modelInputBudget(config.data)}</output>;
}

function settings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  clients.push(client);
  client.setQueryData(['model-profiles'], { items: [], current_config_revision: 'a'.repeat(64) });
  render(<QueryClientProvider client={client}><ModelSettings /><BudgetConsumer /></QueryClientProvider>);
  return client;
}

it('keeps the saved model window when an older background GET arrives after the PUT', async () => {
  let release!: (value: ModelConfig) => void;
  const staleRead = new Promise<ModelConfig>(resolve => { release = resolve; });
  const read = vi.spyOn(api, 'modelSettings').mockResolvedValueOnce(original).mockReturnValueOnce(staleRead);
  const saved: ModelConfig = { ...original, context_capacity: 65536, output_token_budget: 8192 };
  const save = vi.spyOn(api, 'saveModelSettings').mockResolvedValue(saved);
  const client = settings();
  const capacity = await screen.findByLabelText('模型上下文容量');
  expect(screen.getByLabelText('新任务默认输入预算')).toHaveTextContent('28672');
  let refreshing!: Promise<void>;
  act(() => { refreshing = client.refetchQueries({ queryKey, exact: true }); });
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  fireEvent.change(capacity, { target: { value: '65536' } });
  fireEvent.change(screen.getByLabelText('最大输出 Token'), { target: { value: '8192' } });
  fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await screen.findByText('设置已保存，新请求将使用此配置。');
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ context_capacity: 65536, output_token_budget: 8192 }));
  expect(client.getQueryData(queryKey)).toEqual(saved);
  await act(async () => { release(original); await refreshing; });
  expect(client.getQueryData(queryKey)).toEqual(saved);
  expect(screen.getByLabelText('新任务默认输入预算')).toHaveTextContent('57344');
  expect(screen.getByRole('button', { name: '测试连接' })).toBeEnabled();
});

it('does not restore a stale read error after repairing model settings', async () => {
  const unavailable = new ApiError(503, '配置已损坏', undefined, 'MODEL_CONFIG_UNAVAILABLE');
  let reject!: (error: Error) => void;
  const staleRead = new Promise<ModelConfig>((_, fail) => { reject = fail; });
  const read = vi.spyOn(api, 'modelSettings').mockRejectedValueOnce(unavailable).mockReturnValueOnce(staleRead);
  let finishSave!: (value: ModelConfig) => void;
  const save = vi.spyOn(api, 'saveModelSettings').mockReturnValue(new Promise(resolve => { finishSave = resolve; }));
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  const client = settings();
  await screen.findByRole('button', { name: '备份并重置配置' });
  fireEvent.click(screen.getByRole('button', { name: '备份并重置配置' }));
  let refreshing!: Promise<void>;
  act(() => { refreshing = client.refetchQueries({ queryKey, exact: true }); });
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  await act(async () => finishSave(original));
  await screen.findByLabelText('模型上下文容量');
  expect(save).toHaveBeenCalledWith({ mode: 'demo', clear_api_key: true, repair_config: true });
  expect(client.getQueryData(queryKey)).toEqual(original);
  await act(async () => { reject(unavailable); await refreshing; });
  expect(client.getQueryState(queryKey)?.error).toBeNull();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByLabelText('新任务默认输入预算')).toHaveTextContent('28672');
});
