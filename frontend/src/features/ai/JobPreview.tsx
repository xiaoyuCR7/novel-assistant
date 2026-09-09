import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { projectApi, type JobSummary } from '../../lib/api';

// Ephemeral display only: never used as a candidate, a manuscript, or retrieval input.
export function JobPreview({ projectId, job }: { projectId: string; job: JobSummary }) {
  const cache = useQueryClient();
  const preview = useQuery({
    queryKey: ['job-preview', projectId, job.id, job.control_revision],
    queryFn: () => projectApi(projectId).jobPreview(job.id),
    enabled: job.status === 'running',
    refetchInterval: job.status === 'running' ? 1000 : false,
    retry: false,
    gcTime: 0,
  });
  useEffect(() => {
    if (preview.error) void cache.invalidateQueries({ queryKey: ['jobs', projectId] });
  }, [preview.errorUpdatedAt, preview.error, cache, projectId]);
  const value = preview.data;
  return <div className="job-preview" aria-label="生成中预览">
    <p className="subtle">生成中预览 · 未完成，不能写入正文</p>
    {job.status === 'running' && value?.job_id === job.id && value.status === 'running' && value.sequence > 0 &&
      <p className="assistant-prose">{value.text}</p>}
    {value?.truncated && <p className="subtle">预览已截断，完成后可读取完整结果。</p>}
    {preview.error && <p role="status" className="subtle">预览连接中断，正在重连并核对任务状态。不会重新提交任务。</p>}
  </div>;
}
