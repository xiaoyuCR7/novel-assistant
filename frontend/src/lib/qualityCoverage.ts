import { apiError } from './api';

export type CoverageStatus = 'unchecked' | 'stale' | 'pending' | 'confirmed';
export interface CoverageItem {
  chapter_id: string;
  title: string;
  status: CoverageStatus;
  reason: string;
  job_id: string | null;
  checked_at: string | null;
  can_review: boolean;
}
export interface CoveragePage {
  items: CoverageItem[];
  total: number;
  chapter_count: number;
  counts: Record<CoverageStatus, number>;
  next_offset: number | null;
}
export interface CoverageAction {
  action: 'report' | 'review';
  chapter_id: string;
  job_id?: string;
}

export async function readQualityCoverage(projectId: string, status: CoverageStatus | 'all', offset: number, signal?: AbortSignal): Promise<CoveragePage> {
  const query = new URLSearchParams({ status, limit: '30', offset: String(offset) });
  const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/quality/runs/coverage?${query}`, { signal });
  if (!response.ok) throw await apiError(response);
  return response.json();
}
