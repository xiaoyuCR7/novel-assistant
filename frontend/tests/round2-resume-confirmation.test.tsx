import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { JobStatus } from '../src/features/ai/JobStatus';

afterEach(() => vi.restoreAllMocks());

it('uses server send uncertainty even when a provider guard supplied the pause reason', async () => {
  const resume = vi.fn(async () => {});
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<JobStatus job={{ id: 'paused', status: 'recovery_required', task_type: 'chat',
    recovery_reason: 'PROVIDER_CHANGED', replacement_requires_confirmation: true,
    allowed_actions: ['resume', 'cancel'] }} onResume={resume} />);
  expect(screen.getByText(/可能已经完成或计费/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  expect(confirm).toHaveBeenCalledOnce();
  expect(resume).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  await waitFor(() => expect(resume).toHaveBeenCalledExactlyOnceWith(true));
});

it('resumes known failures without an unknown-result confirmation', async () => {
  const resume = vi.fn(async () => {});
  const confirm = vi.spyOn(window, 'confirm');
  render(<JobStatus job={{ id: 'failed', status: 'failed', task_type: 'chat',
    recovery_reason: 'stage_failed', replacement_requires_confirmation: false,
    allowed_actions: ['resume'] }} onResume={resume} />);
  fireEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  await waitFor(() => expect(resume).toHaveBeenCalledExactlyOnceWith(false));
  expect(confirm).not.toHaveBeenCalled();
});
