import { apiError } from './api';

export interface ProjectTodo {
  id: string;
  kind: string;
  title: string;
  detail: string;
  destination: 'ai' | 'quality' | 'write' | 'conflicts';
  chapter_id: string | null;
  chapter_title: string | null;
  job_id: string | null;
  conversation_id: string | null;
  action_label: string;
}

export interface ProjectTodoPage {
  items: ProjectTodo[];
  total: number;
  counts: Record<string, number>;
  next_offset: number | null;
}

export async function fetchProjectTodos(projectId: string, offset: number, signal?: AbortSignal): Promise<ProjectTodoPage> {
  const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/todos?limit=30&offset=${offset}`, { signal });
  if (!response.ok) throw await apiError(response);
  return response.json();
}
