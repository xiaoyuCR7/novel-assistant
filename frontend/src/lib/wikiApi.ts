import { apiError, type AIJob } from './api';

export type WikiKind = 'entity' | 'plot' | 'timeline';
export interface WikiEntry {
  id: string;
  type: WikiKind;
  title: string;
  preview: string;
  aliases: string[];
}
export interface WikiPage {
  items: WikiEntry[];
  total: number;
  has_more: boolean;
}
export interface WikiSource {
  id: string;
  type: string;
  title: string;
  content: string;
  revision: number;
  source_hash: string;
  reason: string;
}
export interface WikiDetail {
  id: string;
  type: WikiKind;
  title: string;
  aliases: string[];
  scope_chapter_id: string | null;
  sources: WikiSource[];
  links: Array<Pick<WikiEntry, 'id' | 'type' | 'title'>>;
  state_history: Array<{
    id: string;
    chapter_id: string;
    chapter_title: string;
    data: Record<string, unknown>;
    revision: number;
  }>;
  state_conflicts?: Array<unknown>;
  fingerprint: string;
  truncated: boolean;
  summary: null | {
    job_id: string;
    stale: boolean;
    claims: Array<{ text: string; source_ids: string[] }>;
    created_at: string;
  };
  job: AIJob | null;
}
export interface WikiSummaryCommand {
  chapter_id: string | null;
  fingerprint: string;
  confirm_unknown?: boolean;
}

export function wikiApi(projectId: string) {
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}/wiki`;
  async function request<T>(url: string, init?: RequestInit): Promise<T> {
    const response = await fetch(url, init);
    if (!response.ok) throw await apiError(response);
    return response.json() as Promise<T>;
  }
  const entryPath = (kind: WikiKind, id: string) => `${base}/${kind}/${encodeURIComponent(id)}`;
  return {
    list: (q: string, kind: WikiKind | 'all', offset: number, chapterId: string | null, signal?: AbortSignal) => {
      const params = new URLSearchParams({ q, kind, offset: String(offset), limit: '30' });
      if (chapterId) params.set('chapter_id', chapterId);
      return request<WikiPage>(`${base}?${params}`, { signal });
    },
    detail: (kind: WikiKind, id: string, chapterId: string | null, signal?: AbortSignal) =>
      request<WikiDetail>(entryPath(kind, id) + (chapterId ? `?chapter_id=${encodeURIComponent(chapterId)}` : ''), { signal }),
    summarize: (kind: WikiKind, id: string, command: WikiSummaryCommand, idempotencyKey: string) =>
      request<AIJob>(`${entryPath(kind, id)}/summarize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify(command),
      }),
  };
}
