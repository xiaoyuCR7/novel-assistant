import { apiError, type AIJob, type JobPage } from './api';
import type { ChapterVersion } from './types';

export type QualityMode = 'polish' | 'collaborate';
export type QualityDimension = 'readability' | 'engagement' | 'pacing' | 'clarity' | 'consistency';
export interface QualityReview {
  scores: Record<QualityDimension, number>;
  summary: string;
  issues: Array<{ dimension: QualityDimension; severity: 'note' | 'warning' | 'error'; quote: string; reason: string; suggestion: string }>;
  next_guidance: string;
  preserves_story: boolean;
}
export interface QualityChapter {
  chapter_id: string;
  title: string;
  source_revision: number;
  draft: string;
  candidate_text: string;
  before: QualityReview | null;
  after: QualityReview | null;
  status: 'writing' | 'reviewing' | 'rewriting' | 'ready' | 'needs_review';
  handoff: { writer_message: string; optimizer_message: string; next_guidance: string };
}
export interface QualityRun extends Omit<AIJob, 'result' | 'effects'> {
  result: {
    mode: QualityMode;
    chapters: QualityChapter[];
    messages: Array<{ role: 'writer' | 'optimizer'; chapter_id: string; kind: 'handoff' | 'feedback'; content: string }>;
    active_chapter_id: string | null;
    active_role: 'writer' | 'optimizer' | null;
    completed_chapters: number;
    total_chapters: number;
  };
  effects?: AIJob['effects'] & { accepted_chapters?: Record<string, string>; quality_approvals?: Record<string, string>; accepted_selections?: Record<string, string[]> };
}
export interface QualityDiff {
  chapter_id: string;
  source_revision: number;
  mode: QualityMode;
  hunks: Array<{ id: string; old_text: string; new_text: string }>;
  coarse?: boolean;
}
export interface QualityCommand {
  mode: QualityMode;
  chapters: Array<{ chapter_id: string; expected_revision: number }>;
  instructions: string;
  token_budget: number;
  quality_target: number;
}

export function qualityApi(projectId: string) {
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}/quality/runs`;
  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(path, init);
    if (!response.ok) throw await apiError(response);
    return response.json() as Promise<T>;
  }
  const path = (id: string) => `${base}/${encodeURIComponent(id)}`;
  const post = <T>(url: string, body: object, key?: string) => request<T>(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) },
    body: JSON.stringify(body),
  });
  return {
    list: (before?: string, activeOnly = false, signal?: AbortSignal) => {
      const query = new URLSearchParams({ limit: '20', active_only: String(activeOnly) });
      if (before) query.set('before', before);
      return request<JobPage>(`${base}?${query}`, { signal });
    },
    detail: (id: string, signal?: AbortSignal) => request<QualityRun>(path(id), { signal }),
    diff: (id: string, chapterId: string, signal?: AbortSignal) => request<QualityDiff>(`${path(id)}/chapters/${encodeURIComponent(chapterId)}/diff`, { signal }),
    start: (command: QualityCommand, key: string) => post<QualityRun>(base, command, key),
    approve: (id: string, chapterId: string, revision: number) => post<QualityRun>(`${path(id)}/approve`, {
      chapter_id: chapterId, expected_control_revision: revision, confirmed: true,
    }),
    accept: async (id: string, chapterId: string, confirmed = false, selectedHunkIds?: string[]) => {
      const version = await post<ChapterVersion>(`${path(id)}/accept`, { chapter_id: chapterId, confirmed,
        ...(selectedHunkIds !== undefined ? { selected_hunk_ids: selectedHunkIds } : {}) });
      if (!version || typeof version.id !== 'string' || !version.id) throw Error('未收到有效采纳回执，请核对任务状态后重试。');
      return version;
    },
  };
}
