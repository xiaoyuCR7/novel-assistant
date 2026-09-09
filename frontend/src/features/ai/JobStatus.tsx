import { useState } from 'react';
import type { JobSummary } from '../../lib/api';
import { hasPublishedSummary } from './jobSemantics';

const stages: Record<string, string> = { context_embedding: '检索参考', plan: '规划', draft: '初稿',
  'review.repair': '修复审校格式与证据', 'continuity_review.repair': '修复连续性审校',
  'style_review.repair': '修复文风审校', 'final_review.repair': '修复最终审校',
  continuity_review: '连续性检查', final_review: '最终连续性复核', style_review: '文风检查', resolve: '修订方案', rewrite: '改写',
  chat: '对话', continue: '续写', chapter_summary: '章节总结', content_check: '内容检查', summary_publish: '总结入库',
  ledger_projection: '更新账本', wiki_summary: '整理 Wiki 摘要' };

export function JobStatus({ job, onCancel, onResume, onRepair, onReplace, replaceDisabled, sourceUnavailable = false }: {
  job: JobSummary; onCancel?: () => Promise<unknown>; onResume?: (confirm: boolean) => Promise<unknown>;
  onRepair?: () => Promise<unknown>;
  onReplace?: () => Promise<unknown>; replaceDisabled?: boolean;
  sourceUnavailable?: boolean;
}) {
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function act(action: () => Promise<unknown>) {
    setBusy(true); setError('');
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  const unknown = !!job.replacement_requires_confirmation || job.recovery_reason === 'result_unknown';
  const summaryNeedsCancel = job.task_type === 'chapter_summary' && job.status !== 'cancelled';
  const publishedSummary = hasPublishedSummary(job);
  const canReplace = !!onReplace && job.allowed_actions?.includes('replace') && !publishedSummary;
  const chunk = job.current_stage?.match(/^chapter_summary\.chunk\.(\d+)$/);
  const merge = job.current_stage?.match(/^chapter_summary\.merge\.(\d+)\.(\d+)$/);
  const stageLabel = chunk ? `分段内容检查（第${Number(chunk[1]) + 1}段）`
    : merge ? `汇总内容检查（第${Number(merge[1]) + 1}轮/第${Number(merge[2]) + 1}组）`
    : job.current_stage ? stages[job.current_stage] ?? job.current_stage : '';
  const contentReview = job.recovery_reason === 'content_review_required';
  const label = contentReview ? '已暂停，等待作者处理内容问题' : ({ queued: '已提交，等待处理', running: '正在处理', cancel_requested: '正在取消',
    cancelled: '已取消', failed: '执行失败', recovery_required: '已暂停，等待作者决定',
    succeeded: '已完成' } as Record<string, string>)[job.status] ?? '任务状态待确认';
  return <div className="job-status">
    <p role="status">{label}{stageLabel ? ` · ${stageLabel}` : ''}</p>
    {unknown && <p className="subtle">上次请求可能已经完成或计费，但没有收到结果。不会自动重发。</p>}
    {contentReview && <p className="subtle">内容检查发现严重问题，处理后可继续完成本章。</p>}
    {publishedSummary && <p className="subtle">AI 总结已保存{job.effects?.ledger_pending ? ' · 账本待修复' : ' · 已更新账本'}</p>}
    {job.error_message && <p className="error-note">{job.error_message}</p>}
    {job.task_type === 'chapter_summary' && job.replaces_job_id && <p className="subtle">原任务：{job.replaces_job_id}</p>}
    {onCancel && job.allowed_actions?.includes('cancel') && <button disabled={busy || job.status === 'cancel_requested'} onClick={() => void act(onCancel)}>取消任务</button>}
    {onResume && job.allowed_actions?.includes('resume') && <button disabled={busy || sourceUnavailable} onClick={() => {
      if (unknown && !window.confirm('上次请求结果未知，恢复可能再次调用模型并产生重复费用。仍要恢复吗？')) return;
      void act(() => onResume(unknown));
    }}>恢复任务</button>}
    {onRepair && publishedSummary && job.effects?.ledger_pending && <button disabled={busy} onClick={() => void act(onRepair)}>仅修复本地账本</button>}
    {canReplace && <button disabled={busy || sourceUnavailable || replaceDisabled || summaryNeedsCancel}
      onClick={() => void act(onReplace!)}>按当前设置新建</button>}
    {canReplace && summaryNeedsCancel && <p className="subtle">先取消原总结任务，再按当前设置新建；旧记录仍会保留。</p>}
    {error && <p role="alert" className="error-note">{error}</p>}
  </div>;
}
