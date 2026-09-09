/** Save request identity before transport. Never save model configuration here. */
export interface PendingSubmission {
  idempotencyKey: string;
  projectId: string;
  chapterId: string | null;
  command: Record<string, unknown>;
  operation: string;
  persisted: boolean;
}

const memory = new Map<string, PendingSubmission>();
export const submissionKey = (project: string, key: string) =>
  `studio:submission:${encodeURIComponent(project)}:${encodeURIComponent(key)}`;

function canonical(value: unknown): string {
  if (!value || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => JSON.stringify(key) + ':' + canonical(item)).join(',') + '}';
}

export function pendingSubmissions(projectId: string): PendingSubmission[] {
  const found = new Map<string, PendingSubmission>();
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)!;
      if (!key.startsWith(`studio:submission:${encodeURIComponent(projectId)}:`)) continue;
      try {
        const item = JSON.parse(localStorage.getItem(key) ?? 'null');
        if (!item || item.projectId !== projectId || typeof item.idempotencyKey !== 'string'
          || !/^[A-Za-z0-9_-]{1,128}$/.test(item.idempotencyKey)
          || !(item.chapterId === null || typeof item.chapterId === 'string')
          || typeof item.operation !== 'string'
          || !item.command || typeof item.command !== 'object' || Array.isArray(item.command)
          || Object.keys(item.command).some(k => /key|secret|token(?!_budget)/i.test(k))
          || key !== submissionKey(projectId, item.idempotencyKey)) continue;
        found.set(key, { ...item, persisted: true });
      } catch { /* Ignore invalid records without overwriting them. */ }
    }
  } catch { /* In-memory identity still protects retries in this page. */ }
  for (const [key, item] of memory) if (item.projectId === projectId) found.set(key, item);
  return [...found.values()];
}

export function prepareSubmission(projectId: string, chapterId: string | null,
  command: Record<string, unknown>, operation = 'writing'): PendingSubmission {
  if (Object.keys(command).some(key => /key|secret|token(?!_budget)/i.test(key)))
    throw Error('待提交记录不能包含密钥或模型配置。');
  const prior = pendingSubmissions(projectId).find(item => item.chapterId === chapterId
    && item.operation === operation && canonical(item.command) === canonical(command));
  if (prior) return prior;
  const item: PendingSubmission = { projectId, chapterId, operation,
    command: JSON.parse(JSON.stringify(command)), idempotencyKey: crypto.randomUUID(), persisted: false };
  const key = submissionKey(projectId, item.idempotencyKey);
  try {
    localStorage.setItem(key, JSON.stringify(item));
    item.persisted = true;
  } catch { memory.set(key, item); }
  return item;
}

export function acknowledge(item: PendingSubmission) {
  const key = submissionKey(item.projectId, item.idempotencyKey);
  memory.delete(key);
  try { localStorage.removeItem(key); } catch { /* Replaying this key remains idempotent. */ }
}
