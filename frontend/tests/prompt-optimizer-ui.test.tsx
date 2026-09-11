import { useRef, useState } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { PromptOptimizer } from '../src/features/ai/PromptOptimizer';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const vague = '帮我优化一下，让文字更吸引人。';
const explicit = '请删除第2章最后两段的重复比喻，保留人物动机与事件顺序，输出修改后的两段正文。';

function setup(options: { initial?: string; disabled?: boolean; hasChapter?: boolean; reject?: boolean } = {}) {
  const replace = vi.fn(), submitted = vi.fn();
  function Host() {
    const [value, setValue] = useState(options.initial ?? vague);
    const [disabled, setDisabled] = useState(options.disabled ?? false);
    const current = useRef(value); current.current = value;
    return <form onSubmit={event => { event.preventDefault(); submitted(); }}>
      <label>消息输入<textarea value={value} onChange={event => setValue(event.target.value)} /></label>
      <button type="button" onClick={() => setValue('')}>模拟发送清空</button>
      <button type="button" onClick={() => setDisabled(!disabled)}>切换只读</button>
      <PromptOptimizer value={value} task="rewrite" hasChapter={options.hasChapter ?? true}
        chapterTitle="第二章 · 雾中来信" disabled={disabled} onReplace={(expected, replacement) => {
          replace(expected, replacement);
          if (options.reject || expected !== current.current) return false;
          setValue(replacement); return true;
        }} />
    </form>;
  }
  const rendered = render(<Host />);
  return { ...rendered, replace, submitted, input: () => screen.getByLabelText('消息输入') as HTMLTextAreaElement };
}

async function open() { await userEvent.click(screen.getByRole('button', { name: '完善提示词' })); }
function complete() {
  fireEvent.change(screen.getByLabelText('具体目标'), { target: { value: '删去重复比喻，让动作更清楚' } });
  fireEvent.change(screen.getByLabelText('处理范围'), { target: { value: '第二章结尾两段' } });
}

it('hints at vague phrases without opening automatically, and previews additions without rewriting the source', async () => {
  const state = setup();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByText(/这些表达可以更具体/)).toBeVisible();
  await open();
  expect(screen.getByRole('dialog', { name: '完善提示词' })).toBeVisible();
  expect(screen.getByRole('region', { name: '原始想法' })).toHaveTextContent(vague);
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeDisabled();
  expect(screen.getByLabelText('具体目标')).toBeRequired();
  expect(screen.getByLabelText('具体目标')).toHaveAccessibleDescription(/请补充具体目标/);
  complete();
  const preview = screen.getByRole('region', { name: '优化后的提示词' });
  expect(preview.querySelector('pre')).not.toHaveAttribute('aria-live');
  expect(preview.querySelector('pre')).not.toHaveAttribute('aria-atomic');
  expect(preview).toHaveTextContent(vague);
  expect(preview).toHaveTextContent('第二章结尾两段');
  fireEvent.change(screen.getByLabelText('具体目标'), { target: { value: '保留克制语气，减少形容词' } });
  expect(preview).toHaveTextContent('保留克制语气，减少形容词');
  expect(preview).not.toHaveTextContent('删去重复比喻，让动作更清楚');
  expect(state.input().value).toBe(vague);
  expect(state.replace).not.toHaveBeenCalled();
});

it('changes templates without silently filling answers and uses suggestions only after a click', async () => {
  setup(); await open();
  fireEvent.change(screen.getByLabelText('提示词模板'), { target: { value: 'polish' } });
  expect(screen.getByLabelText('具体目标')).toHaveValue('');
  const suggestion = within(screen.getByRole('group', { name: '具体目标建议' })).getAllByRole('button')[0];
  const text = suggestion.textContent;
  await userEvent.click(suggestion);
  expect(screen.getByLabelText('具体目标')).toHaveValue(text);
  await userEvent.click(screen.getByRole('button', { name: '使用当前章节' }));
  expect(screen.getByLabelText('处理范围')).toHaveValue('第二章 · 雾中来信');
  fireEvent.change(screen.getByLabelText('提示词模板'), { target: { value: 'review' } });
  expect(screen.getByLabelText('具体目标')).toHaveValue(text);
});

it('does not call an already scoped concrete request vague just because it mentions polishing', () => {
  setup({ initial: '请润色第2章，删除重复比喻。' });
  expect(screen.queryByText(/这些表达可以更具体/)).not.toBeInTheDocument();
});

it('does not offer a current-chapter shortcut in a whole-book conversation', async () => {
  setup({ hasChapter: false }); await open();
  expect(screen.queryByRole('button', { name: '使用当前章节' })).not.toBeInTheDocument();
});

it('closing or pressing Escape keeps the input intact and restores entry focus', async () => {
  const state = setup(); await open(); complete();
  await userEvent.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '完善提示词' })).toHaveFocus();
  expect(state.input().value).toBe(vague);
  expect(state.replace).not.toHaveBeenCalled();
  await open();
  await userEvent.click(screen.getByRole('button', { name: '关闭' }));
  expect(state.submitted).not.toHaveBeenCalled();
});

it('applies explicitly and supports one exact before/after undo and redo without submitting a form', async () => {
  const state = setup(); await open(); complete();
  await userEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  const optimized = state.input().value;
  expect(optimized).toContain(vague);
  expect(optimized).toContain('删去重复比喻，让动作更清楚');
  expect(state.replace).toHaveBeenLastCalledWith(vague, optimized);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '撤销提示词优化' }));
  expect(state.input().value).toBe(vague);
  await userEvent.click(screen.getByRole('button', { name: '恢复提示词优化' }));
  expect(state.input().value).toBe(optimized);
  expect(state.submitted).not.toHaveBeenCalled();
});

it('reopens an applied optimization for revision without nesting old guidance or losing answers', async () => {
  const state = setup(); await open(); complete();
  await userEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  const first = state.input().value;
  await open();
  expect(screen.getByLabelText('具体目标')).toHaveValue('删去重复比喻，让动作更清楚');
  expect(screen.getByLabelText('处理范围')).toHaveValue('第二章结尾两段');
  expect(screen.getByRole('region', { name: '原始想法' }).querySelector('pre')?.textContent).toBe(vague);
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('具体目标'), { target: { value: '减少重复动作，保留克制语气' } });
  const revised = screen.getByRole('region', { name: '优化后的提示词' }).querySelector('pre')!.textContent!;
  expect(revised).not.toContain('删去重复比喻，让动作更清楚');
  expect(revised.match(/【任务说明】/g)).toHaveLength(1);
  expect(revised.match(/【作者补充】/g)).toHaveLength(1);
  await userEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  expect(state.input().value).toBe(revised);
  expect(state.replace).toHaveBeenLastCalledWith(first, revised);
  await userEvent.click(screen.getByRole('button', { name: '撤销提示词优化' }));
  expect(state.input().value).toBe(first);
  await open();
  expect(screen.getByLabelText('具体目标')).toHaveValue('删去重复比喻，让动作更清楚');
  expect(screen.getByRole('region', { name: '原始想法' }).querySelector('pre')?.textContent).toBe(vague);
  await userEvent.click(screen.getByRole('button', { name: '关闭' }));
  await userEvent.click(screen.getByRole('button', { name: '恢复提示词优化' }));
  expect(state.input().value).toBe(revised);
  await open();
  expect(screen.getByLabelText('具体目标')).toHaveValue('减少重复动作，保留克制语气');
  fireEvent.change(state.input(), { target: { value: '最新手动想法，不要覆盖' } });
  fireEvent.change(screen.getByLabelText('具体目标'), { target: { value: '尚未应用的新目标' } });
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('输入已更新');
  expect(state.input().value).toBe('最新手动想法，不要覆盖');
  expect(state.submitted).not.toHaveBeenCalled();
});

it('guards against a changed input and can restart the editor using the latest text', async () => {
  const state = setup(); await open(); complete();
  fireEvent.change(state.input(), { target: { value: explicit } });
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('输入已更新');
  expect(state.replace).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '按最新输入重新开始' }));
  expect(screen.getByRole('region', { name: '原始想法' })).toHaveTextContent(explicit);
  expect(screen.getByLabelText('具体目标')).toHaveValue('');
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeEnabled();
});

it('reports a last-moment replace conflict without claiming an application or losing the original', async () => {
  const state = setup({ reject: true }); await open(); complete();
  await userEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  expect(screen.getByRole('alert')).toHaveTextContent('输入已更新');
  expect(screen.getByRole('dialog')).toBeVisible();
  expect(state.input().value).toBe(vague);
  expect(screen.queryByRole('button', { name: '撤销提示词优化' })).not.toBeInTheDocument();
});

it('never replaces manual edits through undo or redo, and clearing sent input invalidates snapshots', async () => {
  const state = setup(); await open(); complete();
  await userEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  const optimized = state.input().value;
  fireEvent.change(state.input(), { target: { value: optimized + '手动追加' } });
  expect(screen.getByRole('button', { name: '撤销提示词优化' })).toBeDisabled();
  fireEvent.change(state.input(), { target: { value: optimized } });
  await userEvent.click(screen.getByRole('button', { name: '撤销提示词优化' }));
  fireEvent.change(state.input(), { target: { value: '新的手动想法' } });
  expect(screen.getByRole('button', { name: '恢复提示词优化' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '模拟发送清空' }));
  fireEvent.change(state.input(), { target: { value: vague } });
  expect(screen.queryByRole('button', { name: '恢复提示词优化' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '撤销提示词优化' })).not.toBeInTheDocument();
});

it.each(['', ' \n\t ', '字'.repeat(16001)])('blocks empty or over-limit original input %#', async initial => {
  setup({ initial }); await open(); complete();
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeDisabled();
});

it('blocks an oversized result and permits a clearly scoped original without forced answers', async () => {
  setup({ initial: explicit }); await open();
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeEnabled();
  const longAnswer = '限制'.repeat(8001) + '末尾必须保留';
  const answer = screen.getByLabelText('保留与限制');
  expect(answer).not.toHaveAttribute('maxlength');
  fireEvent.change(answer, { target: { value: longAnswer } });
  expect(answer).toHaveValue(longAnswer);
  expect(screen.getByRole('region', { name: '优化后的提示词' })).toHaveTextContent('末尾必须保留');
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeDisabled();
  expect(screen.getByText(/优化后超过/)).toBeVisible();
});

it('allows reading while disabled but blocks application, undo and redo', async () => {
  const state = setup(); await open(); complete();
  await userEvent.click(screen.getByRole('button', { name: '应用到输入框' }));
  await userEvent.click(screen.getByRole('button', { name: '切换只读' }));
  expect(screen.getByRole('button', { name: '撤销提示词优化' })).toBeDisabled();
  await open();
  expect(screen.getByRole('region', { name: '原始想法' }).querySelector('pre')?.textContent).toBe(vague);
  expect(screen.getByRole('button', { name: '应用到输入框' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '关闭' }));
  await userEvent.click(screen.getByRole('button', { name: '切换只读' }));
  await userEvent.click(screen.getByRole('button', { name: '撤销提示词优化' }));
  await userEvent.click(screen.getByRole('button', { name: '切换只读' }));
  expect(screen.getByRole('button', { name: '恢复提示词优化' })).toBeDisabled();
});

it('keeps all optimizer controls non-submitting and uses no network or browser persistence', async () => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
  const storage = vi.spyOn(Storage.prototype, 'setItem');
  const state = setup({ initial: explicit }); await open();
  const dialog = screen.getByRole('dialog');
  for (const button of within(dialog).getAllByRole('button')) expect(button).toHaveAttribute('type', 'button');
  const apply = within(dialog).getByRole('button', { name: '应用到输入框' });
  await userEvent.click(apply);
  await userEvent.click(screen.getByRole('button', { name: '撤销提示词优化' }));
  expect(fetcher).not.toHaveBeenCalled();
  expect(storage).not.toHaveBeenCalled();
  expect(state.submitted).not.toHaveBeenCalled();
});
