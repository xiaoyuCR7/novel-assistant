import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { DiagnosticError } from '../../components/DiagnosticError';
import { readQualityCoverage, type CoverageAction, type CoverageItem, type CoverageStatus } from '../../lib/qualityCoverage';
import './quality-coverage.css';

interface Props { projectId: string; onOpen: (action: CoverageAction) => void | Promise<void> }
const labels: Record<CoverageStatus, string> = { unchecked: '未检查', stale: '已过期', pending: '待处理', confirmed: '已确认' };

export function QualityCoverage(props: Props) { return <CoverageList key={props.projectId} {...props} />; }

function CoverageList({ projectId, onOpen }: Props) {
  const cache = useQueryClient();
  const [status, setStatus] = useState<CoverageStatus | 'all'>('all'), [offset, setOffset] = useState(0);
  const [opening, setOpening] = useState<string | null>(null), [error, setError] = useState<unknown>(null);
  const locked = useRef(false), alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const query = useQuery({ queryKey: ['quality-coverage', projectId, status, offset],
    queryFn: ({ signal }) => readQualityCoverage(projectId, status, offset, signal) });
  const page = query.data;

  async function open(item: CoverageItem, action: CoverageAction['action']) {
    if (locked.current || (action === 'review' && !item.can_review)) return;
    locked.current = true; setOpening(item.chapter_id); setError(null);
    try { await onOpen({ action, chapter_id: item.chapter_id, ...(item.job_id ? { job_id: item.job_id } : {}) }); }
    catch (cause) { if (alive.current) setError(cause); }
    finally { locked.current = false; if (alive.current) setOpening(null); }
  }

  return <section className="quality-coverage" aria-label="整书审校覆盖" aria-busy={query.isFetching}>
    <header><h2>整书审校覆盖</h2><p>沿目录查看每章最近一次质量任务，选择需要复核的章节。</p></header>
    <p className="coverage-note">已确认表示作者完整采纳且当前正文与已知来源匹配。模型评分通过、认可后继续或局部采纳均不等于整章确认。这里仅核对已记录的依赖，不能覆盖全部语义关联或保证文学质量。</p>
    {page && <dl className="coverage-counts">{Object.entries(labels).map(([key, label]) => <div key={key}>
      <dt>{label}</dt><dd>{page.counts[key as CoverageStatus]}</dd></div>)}</dl>}
    <div className="coverage-controls"><label>审校状态<select value={status} disabled={opening != null}
      onChange={event => { setStatus(event.target.value as CoverageStatus | 'all'); setOffset(0); }}>
      <option value="all">全部章节</option>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
    </select></label><button type="button" disabled={query.isFetching || opening != null} onClick={() => {
      setOffset(0); void cache.invalidateQueries({ queryKey: ['quality-coverage', projectId] });
    }}>刷新覆盖状态</button></div>
    {query.isPending && <p role="status">正在核对本地正文与已知来源，不调用模型…</p>}
    {query.error && <DiagnosticError error={query.error} />}
    {error != null && <DiagnosticError error={error} />}
    {page && <>
      <p role="status">全书 {page.chapter_count} 章，当前筛选 {page.total} 章，本页 {page.items.length} 章。</p>
      {page.items.length === 0 && <p>{page.chapter_count === 0 ? '还没有章节。' : '当前筛选下没有章节。'}</p>}
      <ol className="coverage-chapters" start={offset + 1}>{page.items.map(item => <li key={item.chapter_id}>
        <div className="coverage-description"><div className="coverage-title"><h3>{item.title}</h3><span className="coverage-status">{labels[item.status]}</span></div>
          <p>{item.reason}</p>{!item.can_review && <p>请先保存非空且不超过 50,000 字符的正文，再开始复核。</p>}
          {item.checked_at && <small>记录时间：<time dateTime={item.checked_at}>{item.checked_at.slice(0, 16).replace('T', ' ')}</time></small>}
        </div>
        <div className="coverage-actions">{item.job_id && <button type="button" disabled={opening != null}
          onClick={() => void open(item, 'report')}>{item.checked_at ? '查看报告' : '查看任务'}</button>}
          <button type="button" disabled={!item.can_review || opening != null} onClick={() => void open(item, 'review')}>
            {opening === item.chapter_id ? '正在打开…' : '复核本章'}</button></div>
      </li>)}</ol>
      {(offset > 0 || page.next_offset != null) && <nav className="coverage-pagination" aria-label="审校覆盖分页">
        <button type="button" disabled={offset === 0 || query.isFetching || opening != null} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button>
        <span>第 {Math.floor(offset / 30) + 1} 页</span>
        <button type="button" disabled={page.next_offset == null || query.isFetching || opening != null}
          onClick={() => page.next_offset != null && setOffset(page.next_offset)}>下一页</button>
      </nav>}
    </>}
    <p className="coverage-note">浏览不会调用模型。“复核本章”只打开单章优化设置，由你确认后再开始；交替协作仍最多选择 5 章，不会自动扫描整书。</p>
  </section>;
}
