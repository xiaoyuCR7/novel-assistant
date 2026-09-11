import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ChatWorkspace } from '../src/features/ai/ChatWorkspace';

const defaults = { chapterTitle: '灯火归来', hasChapter: true, jobs: [], running: false,
  onAccept: vi.fn(), onOpenManuscript: vi.fn() };
afterEach(() => { localStorage.clear(); window.dispatchEvent(new Event('pagehide')); });

async function prepare() {
  fireEvent.click(screen.getByRole('button', { name: '完善提示词' }));
  fireEvent.change(await screen.findByLabelText('具体目标'), { target: { value: '减少重复叙述，让对话自然' } });
  fireEvent.change(screen.getByLabelText('处理范围'), { target: { value: '第一章已保存的正文' } });
}

it('applies and reverses a prompt in the real composer without sending or changing the writing mode', async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  render(<ChatWorkspace {...defaults} onSend={send} />);
  const input = screen.getByLabelText('给 AI 的消息');
  fireEvent.change(input, { target: { value: '打磨润色文章' } });
  await prepare();
  fireEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  const optimized = (input as HTMLTextAreaElement).value;
  expect(optimized).toContain('打磨润色文章');
  expect(optimized).toContain('减少重复叙述，让对话自然');
  expect(optimized).toContain('第一章已保存的正文');
  expect(send).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: '讨论' })).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(screen.getByRole('button', { name: '撤销提示词优化' }));
  expect(input).toHaveValue('打磨润色文章');
  fireEvent.click(screen.getByRole('button', { name: '恢复提示词优化' }));
  expect(input).toHaveValue(optimized);
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(send).toHaveBeenCalledWith('chat', optimized));
  await waitFor(() => expect(input).toHaveValue(''));
  const restore = screen.queryByRole('button', { name: '恢复提示词优化' });
  if (restore) expect(restore).toBeDisabled();
});

it('does not apply a preview over a newer composer draft', async () => {
  const send = vi.fn();
  render(<ChatWorkspace {...defaults} onSend={send} />);
  const input = screen.getByLabelText('给 AI 的消息');
  fireEvent.change(input, { target: { value: '优化内容' } });
  await prepare();
  fireEvent.change(input, { target: { value: '刚更新的想法，请保留' } });
  fireEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  expect(input).toHaveValue('刚更新的想法，请保留');
  expect(send).not.toHaveBeenCalled();
});

it('preserves an overlong draft and blocks Enter submission until the author shortens it', async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  render(<ChatWorkspace {...defaults} onSend={send} />);
  const input = screen.getByLabelText('给 AI 的消息');
  const original = '原'.repeat(16000) + '末尾不得丢失';
  fireEvent.change(input, { target: { value: original } });
  expect(input).toHaveValue(original);
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled();
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(send).not.toHaveBeenCalled();
  fireEvent.change(input, { target: { value: '请讨论人物动机' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(send).toHaveBeenCalledWith('chat', '请讨论人物动机'));
});

it('retains the optimized draft and undo history after sending fails, and clears them after a successful retry', async () => {
  const send = vi.fn().mockRejectedValueOnce(new Error('连接暂时失败')).mockResolvedValue(undefined);
  render(<ChatWorkspace {...defaults} onSend={send} />);
  const input = screen.getByLabelText('给 AI 的消息') as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: '优化内容' } });
  await prepare();
  fireEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  const optimized = input.value;
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await screen.findByText('连接暂时失败');
  expect(input).toHaveValue(optimized);
  expect(screen.getByRole('button', { name: '撤销提示词优化' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(input).toHaveValue(''));
  expect(send).toHaveBeenNthCalledWith(2, 'chat', optimized);
  expect(screen.queryByRole('button', { name: '撤销提示词优化' })).not.toBeInTheDocument();
});

it('blocks optimization changes during a pending send and preserves the next manually typed message', async () => {
  let finish!: () => void;
  const send = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
  const view = render(<ChatWorkspace {...defaults} onSend={send} />);
  const input = screen.getByLabelText('给 AI 的消息') as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: '优化内容' } });
  await prepare();
  fireEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  const submitted = input.value;
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  view.rerender(<ChatWorkspace {...defaults} running onSend={send} />);
  expect(screen.getByRole('button', { name: '撤销提示词优化' })).toBeDisabled();
  await prepare();
  fireEvent.change(screen.getByLabelText('具体目标'), { target: { value: '尚未提交的新目标' } });
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '关闭' }));
  fireEvent.change(input, { target: { value: '下一条消息，保留这个新想法' } });
  await act(async () => finish());
  view.rerender(<ChatWorkspace {...defaults} onSend={send} />);
  expect(input).toHaveValue('下一条消息，保留这个新想法');
  expect(screen.getByRole('button', { name: '撤销提示词优化' })).toBeDisabled();
  expect(send).toHaveBeenCalledExactlyOnceWith('chat', submitted);
});
