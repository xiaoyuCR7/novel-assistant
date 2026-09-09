import type { JobSummary } from '../../lib/api';

const SUMMARY_BLOCKING_STATUSES = new Set([
  'queued', 'running', 'cancel_requested', 'recovery_required', 'failed',
]);

export function hasPublishedSummary(job: Pick<JobSummary, 'effects'>): boolean {
  const effects: unknown = job.effects;
  if (effects === null || typeof effects !== 'object' || Array.isArray(effects)
    || !Object.prototype.hasOwnProperty.call(effects, 'summary_id')) return false;
  const summaryId = (effects as Record<string, unknown>).summary_id;
  return typeof summaryId === 'string' && summaryId.trim().length > 0;
}

export function blocksSummaryGeneration(job: JobSummary): boolean {
  return SUMMARY_BLOCKING_STATUSES.has(job.status) && !hasPublishedSummary(job);
}
