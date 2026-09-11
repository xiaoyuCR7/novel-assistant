import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { DiagnosticError } from '../../components/DiagnosticError';
import { fetchProjectTodos, type ProjectTodo } from '../../lib/projectTodos';
import './project-transfers.css';

interface Props { projectId: string; onOpen: (item: ProjectTodo) => void | Promise<void> }
const labels: Record<string, string> = { task_recovery: '中断任务', writing_candidate: '写作候选',
  quality_candidate: '优化候选', chapter_completion: '章节完成与总结', conflict: '内容冲突' };

export function ProjectTodos(props: Props) { return <TodoList key={props.projectId} {...props} />; }

function TodoList({ projectId, onOpen }: Props) {
  const [offset, setOffset] = useState(0);
  const [opening, setOpening] = useState<string | null>(null), [error, setError] = useState<unknown>(null);
  const pending = useRef(false), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const query = useQuery({ queryKey: ['project-todos', projectId, offset],
    queryFn: ({ signal }) => fetchProjectTodos(projectId, offset, signal) });
  const page = query.data;
  async function open(item: ProjectTodo) {
    if (pending.current) return;
    pending.current = true; setOpening(item.id); setError(null);
    try { await onOpen(item); }
    catch (reason) { if (mounted.current) setError(reason); }
    finally { pending.current = false; if (mounted.current) setOpening(null); }
  }
  return <section className="project-transfer" aria-label="创作待办" aria-busy={query.isFetching}>
    <header><h2>创作待办</h2><p>汇总全书需要处理的任务、候选与章节，点击后继续原有流程。</p></header>
    <button type="button" disabled={query.isFetching} onClick={() => {
      if (offset) setOffset(0); else void query.refetch();
    }}>刷新待办</button>
    {query.isPending && <p role="status">正在读取全书待办…</p>}
    {query.error && <DiagnosticError error={query.error} />}
    {error != null && <DiagnosticError error={error} />}
    {page && <>
      <p role="status">共 {page.total} 项，本页 {page.items.length} 项。</p>
      {page.total > 0 && <p>{Object.entries(page.counts).map(([kind, count]) => `${labels[kind] ?? kind} ${count}`).join(' · ')}</p>}
      {page.total === 0 && <p>当前没有待处理事项。</p>}
      <ul className="project-todo-list">{page.items.map(item => <li key={item.id}>
        <div><span className="todo-chapter">{item.chapter_title ?? '全书会话'}</span>
          <h3>{item.title}</h3><p>{item.detail}</p></div>
        <button type="button" disabled={opening != null} onClick={() => void open(item)}>
          {opening === item.id ? '正在打开…' : item.action_label}</button>
      </li>)}</ul>
      {(offset > 0 || page.next_offset != null) && <nav aria-label="待办分页" className="todo-pagination">
        <button type="button" disabled={offset === 0 || query.isFetching} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button>
        <span>第 {Math.floor(offset / 30) + 1} 页</span>
        <button type="button" disabled={page.next_offset == null || query.isFetching}
          onClick={() => page.next_offset != null && setOffset(page.next_offset)}>下一页</button>
      </nav>}
    </>}
  </section>;
}
