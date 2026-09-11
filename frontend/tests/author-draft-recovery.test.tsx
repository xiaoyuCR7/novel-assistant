import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ChapterWorkspace } from '../src/features/writing/ChapterWorkspace';
import { ChapterSummaryPanel } from '../src/features/writing/ChapterSummaryPanel';
import { ChatWorkspace } from '../src/features/ai/ChatWorkspace';
import { loadLocalDrafts, saveLocalDraft, useLocalDraft, type LocalDraft } from '../src/lib/draftStore';
import { LocalDraftRecovery } from '../src/components/LocalDraftRecovery';

afterEach(() => { vi.restoreAllMocks(); window.dispatchEvent(new Event('pagehide')); localStorage.clear(); sessionStorage.clear(); });
const document = { content: '正式正文', contract: { purpose: '原目的', forbidden_revelations: ['原秘密'] }, revision: 3, current_version_id: 'v3' };
function chapter(revision = 3, projectId = 'recovery-project', onSave = vi.fn()) {
  return <ChapterWorkspace projectId={projectId} chapterId="chapter-one" title="第一章" document={{ ...document, revision }} saving={false} onSave={onSave} />;
}

it('recovers all chapter draft fields only after author choice and preserves the old base revision', async () => {
  const save = vi.fn();
  const first = render(chapter());
  fireEvent.change(screen.getByLabelText('章节正文'), { target: { value: '未保存正文' } });
  fireEvent.change(screen.getByLabelText('本章目的'), { target: { value: '新的目的' } });
  fireEvent.change(screen.getByLabelText('禁止提前揭示'), { target: { value: '另一秘密\n结局' } });
  first.unmount();
  render(chapter(4, 'recovery-project', save));
  expect(screen.getByLabelText('章节正文')).toHaveValue('正式正文');
  fireEvent.click(await screen.findByRole('button', { name: '恢复章节草稿' }));
  expect(screen.getByLabelText('章节正文')).toHaveValue('未保存正文');
  expect(screen.getByLabelText('本章目的')).toHaveValue('新的目的');
  expect(screen.getByLabelText('禁止提前揭示')).toHaveValue('另一秘密\n结局');
  expect(screen.getByText(/草稿基于修订 3.*当前修订 4/)).toBeVisible();
  expect(save).not.toHaveBeenCalled();
});

it('isolates projects and retains the draft when local storage fails', async () => {
  const save = vi.fn().mockResolvedValue({ ...document, content: '可继续编辑', revision: 4 });
  const first = render(chapter());
  fireEvent.change(screen.getByLabelText('章节正文'), { target: { value: '仅项目一草稿' } });
  first.unmount();
  const second = render(chapter(3, 'other-project', save));
  expect(screen.queryByRole('button', { name: '恢复章节草稿' })).not.toBeInTheDocument();
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
  fireEvent.change(screen.getByLabelText('章节正文'), { target: { value: '可继续编辑' } });
  expect(await screen.findByText(/本机暂存不可用/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '保存工作副本' }));
  await waitFor(() => expect(save).toHaveBeenCalled());
  second.unmount();
});

it('restores edited summary details after remount without saving automatically', async () => {
  const summary = { id: 's1', chapter_id: 'chapter-one', version_id: 'v1', title: '第一章', recap: '旧总结', details: { end_state: '旧状态' }, origin: 'ai_generated', provider: 'demo', status: 'valid', revision: 2, content_hash: 'hash' };
  const save = vi.fn();
  const props = { projectId: 'recovery-project', summary, onSave: save };
  const first = render(<ChapterSummaryPanel {...props} />);
  fireEvent.click(screen.getByRole('button', { name: '编辑总结' }));
  fireEvent.change(screen.getByLabelText('章节总结'), { target: { value: '新总结草稿' } });
  fireEvent.change(screen.getByLabelText('章末状态'), { target: { value: '新状态' } });
  first.unmount();
  render(<ChapterSummaryPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: '恢复总结草稿' }));
  expect(screen.getByLabelText('章节总结')).toHaveValue('新总结草稿');
  expect(screen.getByLabelText('章末状态')).toHaveValue('新状态');
  expect(save).not.toHaveBeenCalled();
});

it('persists unsent chat across a fresh browser session and clears it only after successful send', async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  const props = { projectId: 'recovery-project', chapterId: 'chapter-one', chapterTitle: '第一章', hasChapter: true, jobs: [], running: false, onSend: send, onAccept: vi.fn(), onOpenManuscript: vi.fn() };
  const first = render(<ChatWorkspace {...props} />);
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '未发的想法' } });
  first.unmount(); window.dispatchEvent(new Event('pagehide')); sessionStorage.clear();
  const second = render(<ChatWorkspace {...props} />);
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('');
  fireEvent.click(await screen.findByRole('button', { name: '恢复聊天草稿' }));
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('未发的想法');
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(screen.getByLabelText('给 AI 的消息')).toHaveValue(''));
  second.unmount(); render(<ChatWorkspace {...props} />);
  expect(screen.queryByRole('button', { name: '恢复聊天草稿' })).not.toBeInTheDocument();
});

it('keeps drafts from deleted chapters readable in the project recovery box', async () => {
  const first = render(chapter());
  fireEvent.change(screen.getByLabelText('章节正文'), { target: { value: '已删除章节的心血' } });
  first.unmount();
  render(<LocalDraftRecovery projectId="recovery-project" />);
  expect((screen.getByLabelText('章节恢复稿内容') as HTMLTextAreaElement).value).toContain('已删除章节的心血');
  expect(screen.getByRole('button', { name: '复制章节草稿' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: '保存工作副本' })).not.toBeInTheDocument();
});

it('retains concurrent typing after a save response and removes only the successfully saved draft', async () => {
  let finish!: (value: typeof document) => void;
  const save = vi.fn(() => new Promise<typeof document>(resolve => { finish = resolve; }));
  const first = render(chapter(3, 'recovery-project', save));
  fireEvent.change(screen.getByLabelText('章节正文'), { target: { value: '已经提交' } });
  fireEvent.click(screen.getByRole('button', { name: '保存工作副本' }));
  fireEvent.change(screen.getByLabelText('章节正文'), { target: { value: '提交后的新想法' } });
  await act(async () => finish({ ...document, content: '已经提交', revision: 4 }));
  first.unmount();
  const drafts = loadLocalDrafts('recovery-project').drafts;
  expect(drafts).toHaveLength(1);
  expect(drafts[0]).toMatchObject({ baseRevision: 4, values: { content: '提交后的新想法' } });
});

it('does not clear another editor draft when one editor saves', async () => {
  const save = vi.fn().mockResolvedValue({ ...document, content: '窗口一', revision: 4 });
  const a = render(chapter(3, 'recovery-project', save));
  const b = render(chapter());
  fireEvent.change(within(a.container).getByLabelText('章节正文'), { target: { value: '窗口一' } });
  fireEvent.change(within(b.container).getByLabelText('章节正文'), { target: { value: '窗口二' } });
  fireEvent.click(within(a.container).getByRole('button', { name: '保存工作副本' }));
  await waitFor(() => expect(loadLocalDrafts('recovery-project').drafts).toHaveLength(1));
  expect(loadLocalDrafts('recovery-project').drafts[0].values.content).toBe('窗口二');
});

it('forks a recovered snapshot for each editor so later typing cannot overwrite another restored copy', () => {
  const seed: LocalDraft = { version: 1, id: 'seed', projectId: 'fork-project', chapterId: 'c', kind: 'chapter', baseRevision: 1,
    updatedAt: new Date().toISOString(), values: { content: '恢复原稿', purpose: '', forbidden: '' } };
  saveLocalDraft(seed);
  const options = { projectId: seed.projectId, chapterId: seed.chapterId, kind: seed.kind, baseRevision: 1, dirty: false, values: seed.values };
  const a = renderHook(props => useLocalDraft(props), { initialProps: options });
  const b = renderHook(props => useLocalDraft(props), { initialProps: options });
  act(() => { a.result.current.adopt(seed); b.result.current.adopt(seed); });
  a.rerender({ ...options, dirty: true, values: { ...seed.values, content: '恢复窗口一' } });
  b.rerender({ ...options, dirty: true, values: { ...seed.values, content: '恢复窗口二' } });
  expect(loadLocalDrafts('fork-project').drafts.map(d => d.values.content).sort()).toEqual(['恢复窗口一', '恢复窗口二']);
});

it('isolates conversation drafts and never imports the default session draft into a new conversation', () => {
  const props = { projectId: 'conversations', chapterId: 'c', draftKey: 'legacy-default', chapterTitle: '第一章', hasChapter: true, jobs: [], running: false, onSend: vi.fn(), onAccept: vi.fn(), onOpenManuscript: vi.fn() };
  const first = render(<ChatWorkspace {...props} conversationId="thread-a" />);
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '仅会话 A' } });
  first.unmount(); sessionStorage.setItem('legacy-default', '旧默认会话草稿');
  render(<ChatWorkspace {...props} conversationId="thread-b" />);
  expect(screen.queryByRole('button', { name: '恢复聊天草稿' })).not.toBeInTheDocument();
  expect(sessionStorage.getItem('legacy-default')).toBe('旧默认会话草稿');
});
