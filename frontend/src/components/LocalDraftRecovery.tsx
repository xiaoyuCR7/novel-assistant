import { useEffect, useState } from 'react';
import { loadLocalDrafts, localDraftText, removeLocalDraft, type LocalDraft } from '../lib/draftStore';
import './draft-recovery.css';

const labels = { chapter: '章节', summary: '总结', chat: '聊天' };
export function DraftRecoveryNotice({ drafts, status, onRestore, onDiscard }: {
  drafts: LocalDraft[]; status: 'idle' | 'saved' | 'unavailable';
  onRestore?: (draft: LocalDraft) => void; onDiscard: (draft: LocalDraft) => void;
}) {
  const [notice, setNotice] = useState('');
  return <div className="draft-recovery">
    {status === 'saved' && <p className="subtle" role="status">草稿已暂存本机 · 尚未保存到项目</p>}
    {status === 'unavailable' && <p className="error-note" role="status">本机暂存不可用，当前编辑仍保留；离开前请保存或复制。</p>}
    {!!drafts.length && <p className="subtle">发现 {drafts.length} 份本机恢复稿；恢复只填入编辑器，不会自动提交。</p>}
    {drafts.map(draft => <article key={draft.id}><details>
      <summary>{draft.title ? `${draft.title} · ` : ''}{labels[draft.kind]}恢复稿 · {draft.baseRevision === null ? '未发送' : `基于修订 ${draft.baseRevision}`} · {draft.updatedAt.replace('T', ' ').slice(0, 19)}</summary>
      <textarea aria-label={`${labels[draft.kind]}恢复稿内容`} readOnly value={localDraftText(draft)} rows={5} />
      </details>
      {onRestore && <button type="button" onClick={() => onRestore(draft)}>恢复{labels[draft.kind]}草稿</button>}
      <button type="button" onClick={async () => {
        try { await navigator.clipboard.writeText(localDraftText(draft)); setNotice('草稿已复制。'); }
        catch { setNotice('无法访问剪贴板，请选中恢复稿内容手动复制。'); }
      }}>复制{labels[draft.kind]}草稿</button>
      <button type="button" onClick={() => { if (window.confirm('确定删除这份本机恢复稿？此操作不修改项目正文。')) onDiscard(draft); }}>删除{labels[draft.kind]}恢复稿</button>
    </article>)}
    {notice && <p role="status">{notice}</p>}
  </div>;
}
export function LocalDraftRecovery({ projectId, onOpenChapter }: { projectId: string; onOpenChapter?: (id: string) => void }) {
  const [result, setResult] = useState(() => loadLocalDrafts(projectId));
  useEffect(() => {
    const refresh = () => setResult(loadLocalDrafts(projectId));
    refresh(); window.addEventListener('storage', refresh); window.addEventListener('studio-local-drafts-changed', refresh);
    return () => { window.removeEventListener('storage', refresh); window.removeEventListener('studio-local-drafts-changed', refresh); };
  }, [projectId]);
  return <section aria-label="本机恢复稿">
    <h2>本机恢复稿</h2><p className="subtle">即使章节已删除，这里的草稿也可复制。清理浏览器数据会移除本机暂存。</p>
    {!result.drafts.length && !result.unavailable && <p>暂无本机恢复稿。</p>}
    <DraftRecoveryNotice drafts={result.drafts} status={result.unavailable ? 'unavailable' : 'idle'}
      onDiscard={draft => { if (removeLocalDraft(draft)) setResult(loadLocalDrafts(projectId)); else setResult(current => ({ ...current, unavailable: true })); }} />
    {onOpenChapter && Array.from(new Set(result.drafts.map(d => d.chapterId).filter((id): id is string => !!id))).map(id =>
      <button key={id} type="button" onClick={() => onOpenChapter(id)}>打开 {result.drafts.find(d => d.chapterId === id)?.title ?? '对应章节'}</button>)}
  </section>;
}
