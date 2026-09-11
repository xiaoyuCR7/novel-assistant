import { apiError } from './api';

export interface ConversationThread {
  id: string;
  project_id: string;
  chapter_id: string | null;
  title: string;
  status: 'active' | 'archived' | 'deleted';
  revision: number;
  is_default: boolean;
  parent_conversation_id: string | null;
  branch_from_job_id: string | null;
  created_at: string;
  updated_at: string;
  job_count: number;
}
export interface ConversationPage {
  items: ConversationThread[];
  next_cursor: string | null;
  default_conversation_id: string;
}
export interface ConversationCreate { id: string; chapter_id?: string | null; title: string }
export interface ConversationUpdate {
  expected_revision: number;
  title?: string;
  status?: ConversationThread['status'];
}

export function conversationApi(projectId: string) {
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}/conversations`;
  const path = (id: string) => `${base}/${encodeURIComponent(id)}`;
  async function request<T>(url: string, init?: RequestInit): Promise<T> {
    const response = await fetch(url, init);
    if (!response.ok) throw await apiError(response);
    return response.json() as Promise<T>;
  }
  const validThread = (value: unknown): value is ConversationThread => {
    if (!value || typeof value !== 'object') return false;
    const row = value as ConversationThread;
    return typeof row.id === 'string' && !!row.id && row.project_id === projectId
      && (row.chapter_id === null || typeof row.chapter_id === 'string')
      && typeof row.title === 'string' && !!row.title.trim() && typeof row.is_default === 'boolean'
      && Number.isInteger(row.revision) && row.revision >= 1
      && ['active', 'archived', 'deleted'].includes(row.status)
      && Number.isInteger(row.job_count) && row.job_count >= 0;
  };
  const mutate = async (url: string, method: string, body: object) => {
    const result = await request<ConversationThread>(url, { method,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!validThread(result) || ('id' in body && result.id !== body.id)) {
      throw Error('未收到有效会话回执，请核对并重试原操作。');
    }
    return result;
  };
  return {
    list: async (options: { chapterId?: string | null; status?: ConversationThread['status']; q?: string;
      before?: string; limit?: number } = {}, signal?: AbortSignal) => {
      const query = new URLSearchParams({ status: options.status ?? 'active', limit: String(options.limit ?? 30) });
      if (options.chapterId) query.set('chapter_id', options.chapterId);
      if (options.q) query.set('q', options.q);
      if (options.before) query.set('before', options.before);
      const page = await request<ConversationPage>(`${base}?${query}`, { signal });
      if (!page || !Array.isArray(page.items) || !page.items.every(validThread)
        || !page.items.every(row => row.chapter_id === (options.chapterId ?? null) && row.status === (options.status ?? 'active'))
        || typeof page.default_conversation_id !== 'string' || !page.default_conversation_id
        || (page.next_cursor !== null && typeof page.next_cursor !== 'string')) {
        throw Error('会话列表回执无效，请刷新后重试。');
      }
      return page;
    },
    detail: async (id: string, signal?: AbortSignal) => {
      const row = await request<ConversationThread>(path(id), { signal });
      if (!validThread(row) || row.id !== id) throw Error('会话详情回执无效，请刷新后重试。');
      return row;
    },
    create: (body: ConversationCreate) => mutate(base, 'POST', body),
    update: (id: string, body: ConversationUpdate) => mutate(path(id), 'PATCH', body),
    branch: (id: string, body: ConversationCreate & { from_job_id: string }) => mutate(`${path(id)}/branches`, 'POST', body),
  };
}
