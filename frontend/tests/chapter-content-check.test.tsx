import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';

import { JobStatus } from '../src/features/ai/JobStatus';
import { DriftAlertCenter } from '../src/features/conflicts/DriftAlertCenter';

it.each([
  ['chapter_summary.chunk.0', '分段内容检查（第1段）'],
  ['chapter_summary.merge.1.2', '汇总内容检查（第2轮/第3组）'],
  ['content_check', '内容检查'],
])('shows content-check-aware summary progress for %s', (stage, label) => {
  render(<JobStatus job={{ id: 'job', status: 'running', task_type: 'chapter_summary', current_stage: stage }} />);
  expect(screen.getByRole('status')).toHaveTextContent(`正在处理 · ${label}`);
});

it('explains a content review pause without unknown-result confirmation', async () => {
  const resume = vi.fn().mockResolvedValue(undefined);
  const confirm = vi.spyOn(window, 'confirm');
  render(<JobStatus job={{ id: 'job', status: 'recovery_required', task_type: 'chapter_summary',
    recovery_reason: 'content_review_required', allowed_actions: ['resume'] }} onResume={resume} />);

  expect(screen.getByRole('status')).toHaveTextContent('等待作者处理内容问题');
  expect(screen.getByText('内容检查发现严重问题，处理后可继续完成本章。')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  expect(confirm).not.toHaveBeenCalled();
  expect(resume).toHaveBeenCalledWith(false);
});

it('renders content evidence and suggestion but hides internal audit tags', () => {
  render(<DriftAlertCenter alerts={[{
    id: 'finding', severity: 'warning', status: 'open', message: '本段偏离章节目的。',
    evidence: [
      'content_check:v1', 'ai_job:job-one', 'document_revision:2',
      'dimension:chapter_purpose', 'range:3:8', 'quote:绕开钟楼',
      'suggestion:补充绕行与投递目标的因果联系。',
    ],
  }]} onDecide={async () => {}} />);

  expect(screen.getByText('章节目的')).toBeVisible();
  expect(screen.getByText('原文：绕开钟楼')).toBeVisible();
  expect(screen.getByText('建议：补充绕行与投递目标的因果联系。')).toBeVisible();
  expect(screen.queryByText(/ai_job:/)).not.toBeInTheDocument();
  expect(screen.queryByText(/document_revision:/)).not.toBeInTheDocument();
  expect(screen.queryByText(/range:/)).not.toBeInTheDocument();
});
