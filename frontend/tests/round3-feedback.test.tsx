import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { StyleLab } from '../src/features/style/StyleLab';
import { FeedbackPanel } from '../src/features/feedback/FeedbackPanel';
import { projectApi } from '../src/lib/api';

afterEach(() => vi.restoreAllMocks());

it('requires an explicit instruction before confirming archived corrections', async () => {
  const confirm = vi.fn().mockResolvedValue(undefined);
  render(<StyleLab candidates={[{ id: 'c1', instruction: '', category: 'general',
    status: 'candidate' }]} rules={[]} onConfirm={confirm} onDisable={vi.fn()} />);
  const button = screen.getByRole('button', { name: '确认风格规则' });
  expect(button).toBeDisabled();
  const input = screen.getByRole('textbox', { name: '补充风格偏好' });
  fireEvent.change(input, { target: { value: '   ' } });
  expect(button).toBeDisabled();
  fireEvent.change(input, { target: { value: '  Use short sentences.  ' } });
  fireEvent.click(button);
  await waitFor(() => expect(confirm).toHaveBeenCalledWith('c1', 'Use short sentences.'));
});

it('keeps the instruction draft when the linked rule must be restored from trash', async () => {
  const confirm = vi.fn().mockRejectedValue(new Error('关联规则已删除，请先到回收站恢复。'));
  render(<StyleLab candidates={[{ id: 'c1', instruction: '', category: 'general',
    status: 'candidate' }]} rules={[]} onConfirm={confirm} onDisable={vi.fn()} />);
  fireEvent.change(screen.getByRole('textbox', { name: '补充风格偏好' }), {
    target: { value: 'Preserve this draft.' },
  });
  fireEvent.click(screen.getByRole('button', { name: '确认风格规则' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('回收站');
  expect(screen.getByRole('textbox', { name: '补充风格偏好' })).toHaveValue('Preserve this draft.');
});

it('passes the optional author instruction through the project API', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }));
  await projectApi('p1').confirmPreference('c1', 'Use short sentences.');
  expect(fetcher).toHaveBeenCalledWith('/api/v1/projects/p1/feedback/preferences/c1/confirm',
    expect.objectContaining({ body: JSON.stringify({ instruction: 'Use short sentences.' }) }));
});

it('explains that corrections alone are archived rather than learned', () => {
  render(<FeedbackPanel originalText="Original" onSubmit={vi.fn()} />);
  expect(screen.getByText(/未填写修改原因.*存档/)).toBeVisible();
});
