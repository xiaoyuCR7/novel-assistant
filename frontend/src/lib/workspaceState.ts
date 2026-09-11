/** Navigation IDs only. Conversation text remains in the project's server storage. */
export interface WorkspaceState {
  view?: string;
  selectedNodeId?: string;
  jobSelections?: Record<string, string>;
  qualityRunId?: string;
  conversationSelections?: Record<string, string>;
}
const key = (projectId: string) => `studio:workspace:${encodeURIComponent(projectId)}`;
const identifier = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512;
function sanitize(value: unknown): WorkspaceState {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const source = value as Record<string, unknown>;
  const result: WorkspaceState = {};
  for (const field of ['view', 'selectedNodeId', 'qualityRunId'] as const) if (identifier(source[field])) result[field] = source[field];
  for (const field of ['jobSelections', 'conversationSelections'] as const) {
    if (source[field] && typeof source[field] === 'object' && !Array.isArray(source[field])) {
      result[field] = Object.fromEntries(Object.entries(source[field])
        .filter(([scope, id]) => identifier(scope) && identifier(id)).slice(-100));
    }
  }
  return result;
}
export function readWorkspaceState(projectId: string): WorkspaceState {
  try {
    const value = JSON.parse(localStorage.getItem(key(projectId)) ?? 'null');
    return value?.version === 1 ? sanitize(value) : {};
  } catch { return {}; }
}
export function saveWorkspaceState(projectId: string, patch: WorkspaceState): void {
  try {
    // Merge the latest record so independently mounted workspaces cannot erase each other's IDs.
    localStorage.setItem(key(projectId), JSON.stringify({ version: 1, ...sanitize({ ...readWorkspaceState(projectId), ...patch }) }));
  } catch { /* Navigation remains usable when storage is unavailable. */ }
}
export function saveWorkspaceJob(projectId: string, scope: string, id?: string): void {
  const selections = { ...readWorkspaceState(projectId).jobSelections };
  delete selections[scope];
  if (id) selections[scope] = id;
  saveWorkspaceState(projectId, { jobSelections: selections });
}
export function saveWorkspaceConversation(projectId: string, scope: string, id: string): void {
  saveWorkspaceState(projectId, { conversationSelections: { ...readWorkspaceState(projectId).conversationSelections, [scope]: id } });
}
