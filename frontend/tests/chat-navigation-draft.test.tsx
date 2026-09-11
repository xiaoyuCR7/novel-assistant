import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ChatWorkspace } from '../src/features/ai/ChatWorkspace';
import { loadLocalDrafts, removeLocalDraft } from '../src/lib/draftStore';

afterEach(() => {
  cleanup(); window.dispatchEvent(new Event('pagehide'));
  vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear();
});
const defaults = { projectId: 'navigation-project', chapterId: 'chapter', chapterTitle: '第一章',
  draftKey: 'legacy:project:chapter', hasChapter: true, jobs: [], running: false,
  onAccept: vi.fn(), onOpenManuscript: vi.fn() };

it('keeps the exact unsent message and action when preparation blocks sending and navigation remounts the composer', async () => {
  const send = vi.fn().mockResolvedValue(undefined), before = vi.fn().mockResolvedValue(false);
  const first = render(<ChatWorkspace {...defaults} onSend={send} beforeSend={before} />);
  fireEvent.click(screen.getByRole('button', { name: '续写' }));
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '从失真的求救信号切入' } });
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(before).toHaveBeenCalledWith('continue'));
  first.unmount();
  const returned = render(<ChatWorkspace {...defaults} onSend={send} />);
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('从失真的求救信号切入');
  expect(screen.getByRole('button', { name: '续写' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.queryByRole('button', { name: '恢复聊天草稿' })).not.toBeInTheDocument();
  expect(loadLocalDrafts(defaults.projectId).drafts).toHaveLength(1);
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(send).toHaveBeenCalledWith('continue', '从失真的求救信号切入'));
  await waitFor(() => expect(screen.getByLabelText('给 AI 的消息')).toHaveValue(''));
  returned.unmount();
  render(<ChatWorkspace {...defaults} onSend={send} />);
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('');
  expect(loadLocalDrafts(defaults.projectId).drafts).toHaveLength(0);
});

it('keeps navigation drafts isolated by conversation, chapter and project', () => {
  const send = vi.fn();
  const first = render(<ChatWorkspace {...defaults} conversationId="a" onSend={send} />);
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '仅会话 A 的草稿' } });
  first.unmount();
  for (const scope of [{ conversationId: 'b' }, { conversationId: 'a', chapterId: 'other' }, { conversationId: 'a', projectId: 'other' }]) {
    const other = render(<ChatWorkspace {...defaults} {...scope} onSend={send} />);
    expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('');
    other.unmount();
  }
  render(<ChatWorkspace {...defaults} conversationId="a" onSend={send} />);
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('仅会话 A 的草稿');
  expect(send).not.toHaveBeenCalled();
});

it('requires explicit recovery after leaving the browser page even while local and session storage survive', () => {
  const send = vi.fn();
  const first = render(<ChatWorkspace {...defaults} onSend={send} />);
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '刷新后待恢复' } });
  first.unmount();
  window.dispatchEvent(new Event('pagehide'));
  render(<ChatWorkspace {...defaults} onSend={send} />);
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('');
  fireEvent.click(screen.getByRole('button', { name: '恢复聊天草稿' }));
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('刷新后待恢复');
  expect(send).not.toHaveBeenCalled();
});

it('keeps same-page navigation usable when browser storage is unavailable', () => {
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
  const first = render(<ChatWorkspace {...defaults} onSend={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '仍可继续编辑' } });
  first.unmount();
  render(<ChatWorkspace {...defaults} onSend={vi.fn()} />);
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('仍可继续编辑');
  expect(screen.getByText(/本机暂存不可用/)).toBeVisible();
});

it('does not revive a navigation draft that the author deleted from the recovery box', () => {
  const first = render(<ChatWorkspace {...defaults} onSend={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '主动删除的草稿' } });
  first.unmount();
  const draft = loadLocalDrafts(defaults.projectId).drafts[0];
  expect(removeLocalDraft(draft)).toBe(true);
  render(<ChatWorkspace {...defaults} onSend={vi.fn()} />);
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('');
  expect(loadLocalDrafts(defaults.projectId).drafts).toHaveLength(0);
});
