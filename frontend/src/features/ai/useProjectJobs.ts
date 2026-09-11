import { useMemo, useRef } from 'react';
import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query';
import { projectApi } from '../../lib/api';
import type { JobPage, JobSummary } from '../../lib/api';

const nonTerminal = (job: JobSummary) => ['queued', 'running', 'cancel_requested', 'recovery_required'].includes(job.status);

export function pollInterval(statuses: string[], unchanged: number): number | false {
  return statuses.some(s => ['queued', 'running', 'cancel_requested'].includes(s))
    ? Math.min(1000 * 2 ** Math.min(unchanged, 3), 8000) : false;
}

export function useProjectJobs(projectId: string, chapterId?: string, kind: 'writing' | 'summary' = 'writing', conversationId?: string, enabled = true) {
  const observed = useRef({ sample: '', fingerprint: '', unchanged: 0 });
  const cache = useQueryClient();
  const historyKey = ['jobs', projectId, chapterId, kind, ...(conversationId ? [conversationId] : [])];
  const activeKey = [...historyKey, 'active'];
  const query = useInfiniteQuery({
    queryKey: historyKey,
    queryFn: ({ pageParam }) => projectApi(projectId).jobsPage(chapterId, kind, pageParam, false, conversationId),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: page => page.next_cursor ?? undefined,
    enabled: enabled && (kind !== 'summary' || !!chapterId),
  });
  const active = useQuery({
    queryKey: activeKey,
    enabled: enabled && (kind !== 'summary' || !!chapterId),
    queryFn: async () => {
      const api = projectApi(projectId);
      const items: JobSummary[] = [];
      const cursors = new Set<string>();
      let before: string | undefined;
      do {
        const page = await api.jobsPage(chapterId, kind, before, true, conversationId);
        items.push(...page.items);
        before = page.next_cursor ?? undefined;
        if (before && cursors.has(before)) throw new Error('任务分页游标重复，请刷新重试。');
        if (before) cursors.add(before);
      } while (before);
      const ids = new Set(items.map(job => job.id));
      const previous = cache.getQueryData<JobSummary[]>(activeKey) ?? [];
      const known = new Map(previous.map(job => [job.id, job]));
      const history = cache.getQueryData<InfiniteData<JobPage>>(historyKey);
      for (const job of history?.pages.flatMap(page => page.items).filter(nonTerminal) ?? []) {
        const prior = known.get(job.id);
        if (!prior || (job.control_revision ?? 0) > (prior.control_revision ?? 0) ||
            (job.updated_at ?? '') > (prior.updated_at ?? '')) known.set(job.id, job);
      }
      const terminal: JobSummary[] = [];
      for (const prior of known.values()) {
        if (ids.has(prior.id)) continue;
        if (nonTerminal(prior)) {
          // Fetch a full result only once on completion, never on each status poll.
          const job = await api.job(prior.id, conversationId);
          cache.setQueryData(['job-detail', projectId, job.id, job.status,
            job.control_revision, job.accepted_version_id], job);
          const summary: JobSummary = { ...prior, status: job.status, updated_at: job.updated_at,
            control_revision: job.control_revision, allowed_actions: job.allowed_actions,
            accepted_version_id: job.accepted_version_id, error_code: job.error_code,
            error_message: job.error_message, effects: job.effects, recovery_reason: job.recovery_reason,
            current_stage: job.current_stage,
            preview: String(job.result?.reply ?? job.result?.candidate_text ?? '').slice(0, 500) };
          items.push(summary);
          if (!nonTerminal(summary)) {
            // Reconcile the loaded page locally; do not poll history or refetch this result
            // again after it falls out of the bounded retained-terminal cache.
            cache.setQueryData<InfiniteData<JobPage>>(historyKey, current => current && ({
              ...current, pages: current.pages.map(page => ({ ...page,
                items: page.items.map(item => item.id === summary.id ? summary : item),
              })),
            }));
          }
        } else terminal.push(prior);
      }
      return [...items.filter(nonTerminal), ...[...terminal, ...items.filter(job => !nonTerminal(job))].slice(-100)];
    },
    refetchInterval: query => {
      const jobs = query.state.data ?? [];
      // React Query also evaluates this callback on renders and observer updates.
      // Advance backoff once per successful data commit, not per evaluation.
      const sample = JSON.stringify([query.queryHash, query.state.dataUpdateCount, query.state.dataUpdatedAt]);
      if (query.state.status === 'success' && sample !== observed.current.sample) {
        const fingerprint = JSON.stringify([projectId, chapterId, kind, conversationId,
          jobs.map(job => [job.id, job.status, job.current_stage, job.control_revision])]);
        observed.current = fingerprint === observed.current.fingerprint
          ? { sample, fingerprint, unchanged: observed.current.unchanged + 1 }
          : { sample, fingerprint, unchanged: 0 };
      }
      return pollInterval(jobs.map(job => job.status), observed.current.unchanged) || 15000;
    },
  });
  const data = useMemo(() => {
    if (!query.data && !active.data) return undefined;
    const merged = new Map<string, JobSummary>();
    for (const job of [...(query.data?.pages.flatMap(page => page.items) ?? []), ...(active.data ?? [])]) {
      const prior = merged.get(job.id);
      if (!prior || (job.updated_at ?? '') >= (prior.updated_at ?? '')) merged.set(job.id, job);
    }
    return [...merged.values()].sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? '') || a.id.localeCompare(b.id));
  }, [query.data, active.data]);
  return { ...query, data, error: query.error ?? active.error };
}
