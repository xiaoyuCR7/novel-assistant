import { apiError } from './api';
import type { Project } from './types';

export interface BackupPreview {
  project_id: string;
  title: string;
  format_version: 0 | 1;
  verified: boolean;
  counts: { chapters: number; versions: number; jobs: number; assets: number };
  conflict: boolean;
  warnings: string[];
  archive_hash: string;
}

export interface ManuscriptCommand {
  format: 'txt' | 'markdown';
  source: 'working' | 'published';
  node_ids: string[];
  order: 'story' | 'selection';
}

async function backupRequest<T>(action: string, file: File, hash?: string, signal?: AbortSignal): Promise<T> {
  const body = new FormData(); body.append('file', file);
  if (hash) body.append('archive_hash', hash);
  const response = await fetch(`/api/v1/projects/backup/${action}`, { method: 'POST', body, signal });
  if (!response.ok) throw await apiError(response);
  return response.json() as Promise<T>;
}

export const previewBackup = (file: File, signal?: AbortSignal) =>
  backupRequest<BackupPreview>('preview', file, undefined, signal);
export const restoreBackup = (file: File, hash: string) =>
  backupRequest<Project>('restore', file, hash);

export async function exportManuscript(projectId: string, command: ManuscriptCommand, signal?: AbortSignal) {
  const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/manuscript/export`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(command), signal,
  });
  if (!response.ok) throw await apiError(response);
  return response.blob();
}
