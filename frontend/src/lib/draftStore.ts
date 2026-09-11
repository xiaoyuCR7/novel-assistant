import { useCallback, useEffect, useRef, useState } from 'react';

export type DraftKind = 'chapter' | 'summary' | 'chat';
export interface LocalDraft {
  version: 1;
  id: string;
  projectId: string;
  chapterId: string | null;
  kind: DraftKind;
  conversationId?: string;
  title?: string;
  baseRevision: number | null;
  baseId?: string;
  updatedAt: string;
  values: Record<string, unknown>;
}
const prefix = 'studio:recovery:v1:';
const changed = 'studio-local-drafts-changed';
const key = (draft: LocalDraft) => `${prefix}${encodeURIComponent(draft.projectId)}:${encodeURIComponent(draft.chapterId ?? '')}:${draft.kind}${draft.conversationId ? `:${encodeURIComponent(draft.conversationId)}` : ''}:${draft.baseRevision ?? ''}:${draft.id}`;
function valid(value: unknown): value is LocalDraft {
  if (!value || typeof value !== 'object') return false;
  const d = value as LocalDraft;
  if (d.version !== 1 || typeof d.id !== 'string' || typeof d.projectId !== 'string'
    || !(d.chapterId === null || typeof d.chapterId === 'string') || typeof d.updatedAt !== 'string'
    || (d.conversationId !== undefined && typeof d.conversationId !== 'string')
    || (d.title !== undefined && typeof d.title !== 'string')
    || !(d.baseRevision === null || (Number.isSafeInteger(d.baseRevision) && d.baseRevision >= 0))
    || !d.values || typeof d.values !== 'object' || Array.isArray(d.values)) return false;
  const v = d.values;
  if (d.kind === 'chapter') return ['content', 'purpose', 'forbidden'].every(k => typeof v[k] === 'string');
  if (d.kind === 'chat') return typeof v.message === 'string' && ['chat', 'plan', 'continue', 'rewrite', 'review'].includes(String(v.task));
  if (d.kind === 'summary') return typeof d.baseId === 'string' && typeof v.recap === 'string' && !!v.details
    && typeof v.details === 'object' && !Array.isArray(v.details)
    && Object.values(v.details).every(item => typeof item === 'string' || (Array.isArray(item) && item.every(entry => typeof entry === 'string')));
  return false;
}
export function loadLocalDrafts(projectId: string, chapterId?: string | null, kind?: DraftKind, conversationId?: string): { drafts: LocalDraft[]; unavailable: boolean } {
  const drafts: LocalDraft[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const name = localStorage.key(i);
      if (!name?.startsWith(`${prefix}${encodeURIComponent(projectId)}:`)) continue;
      let draft: unknown;
      try { draft = JSON.parse(localStorage.getItem(name) ?? 'null'); } catch { continue; }
      if (valid(draft) && key(draft) === name && draft.projectId === projectId
        && (chapterId === undefined || draft.chapterId === chapterId) && (!kind || draft.kind === kind)
        && (kind !== 'chat' || draft.conversationId === conversationId)) drafts.push(draft);
    }
    return { drafts: drafts.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)), unavailable: false };
  } catch { return { drafts, unavailable: true }; }
}
export function saveLocalDraft(draft: LocalDraft): boolean {
  try {
    const data = JSON.stringify(draft);
    if (!valid(draft) || data.length > 2_000_000) return false;
    localStorage.setItem(key(draft), data);
    window.dispatchEvent(new Event(changed));
    return true;
  } catch { return false; }
}
export function removeLocalDraft(draft: LocalDraft): boolean {
  try { localStorage.removeItem(key(draft)); window.dispatchEvent(new Event(changed)); return true; }
  catch { return false; }
}
export function localDraftText(draft: LocalDraft): string {
  if (draft.kind === 'chapter') return `${draft.values.content}\n\n本章目的：${draft.values.purpose}\n禁止提前揭示：\n${draft.values.forbidden}`;
  if (draft.kind === 'chat') return String(draft.values.message);
  return `${draft.values.recap}\n\n${JSON.stringify(draft.values.details, null, 2)}`;
}
export function useLocalDraft(options: {
  projectId?: string; chapterId: string | null; kind: DraftKind; baseRevision?: number;
  conversationId?: string; title?: string;
  initialDraft?: LocalDraft;
  baseId?: string; values: Record<string, unknown>; dirty: boolean;
}) {
  const { projectId, chapterId, kind, baseRevision, baseId, dirty, conversationId, title } = options;
  const serialized = JSON.stringify(options.values);
  const owned = useRef<LocalDraft | null>(options.initialDraft ?? null);
  const [status, setStatus] = useState<'idle' | 'saved' | 'unavailable'>('idle');
  const [candidates, setCandidates] = useState<LocalDraft[]>([]);
  const refresh = useCallback(() => {
    if (!projectId) return;
    const result = loadLocalDrafts(projectId, chapterId, kind, conversationId);
    setCandidates(result.drafts.filter(item => item.id !== owned.current?.id));
    if (result.unavailable) setStatus('unavailable');
  }, [projectId, chapterId, kind, conversationId]);
  useEffect(() => {
    refresh(); window.addEventListener('storage', refresh); window.addEventListener(changed, refresh);
    return () => { window.removeEventListener('storage', refresh); window.removeEventListener(changed, refresh); };
  }, [refresh]);
  useEffect(() => {
    if (!projectId) return;
    if (!dirty) {
      if (owned.current) {
        if (!removeLocalDraft(owned.current)) { setStatus('unavailable'); return; }
        owned.current = null;
        setStatus('idle');
      }
      return;
    }
    const previous = owned.current;
    const draft: LocalDraft = { version: 1, id: previous?.id ?? crypto.randomUUID(), projectId, chapterId,
      kind, conversationId, title, baseRevision: baseRevision ?? null, baseId, values: JSON.parse(serialized), updatedAt: new Date().toISOString() };
    owned.current = draft;
    const saved = saveLocalDraft(draft);
    if (saved && previous && key(previous) !== key(draft)) removeLocalDraft(previous);
    setStatus(saved ? 'saved' : 'unavailable');
  }, [projectId, chapterId, kind, baseRevision, baseId, serialized, dirty, conversationId, title]);
  return { candidates, status, snapshot: () => owned.current,
    adopt: (draft: LocalDraft) => {
      const copy = { ...draft, id: crypto.randomUUID(), updatedAt: new Date().toISOString() };
      owned.current = copy;
      if (saveLocalDraft(copy)) { removeLocalDraft(draft); setStatus('saved'); }
      else setStatus('unavailable');
      refresh();
    },
    discard: (draft: LocalDraft) => { if (!removeLocalDraft(draft)) setStatus('unavailable'); refresh(); },
    discardCurrent: () => {
      if (owned.current && !removeLocalDraft(owned.current)) { setStatus('unavailable'); return false; }
      owned.current = null; setStatus('idle'); return true;
    },
  };
}
