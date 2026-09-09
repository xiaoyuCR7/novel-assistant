import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { api, ApiError } from "../../lib/api";
import type {
  ImportCategory,
  ImportChapterPreview,
  ImportCommitResponse,
  ImportDraft,
  ImportDraftPatch,
  ImportFilePreview,
  ImportLimits,
  ImportSourceKind,
} from "../../lib/types";

const categoryLabels: Record<ImportCategory, string> = {
  manuscript: "正文",
  task: "任务说明",
  outline: "大纲",
  world: "世界观",
  character: "角色资料",
  style: "风格参考",
  other: "其他资料",
};

const steps = ["选择来源", "检查分类与章节", "确认续写点", "创建项目"] as const;

function sourceName(file: File, sourceKind: ImportSourceKind) {
  if (sourceKind === "folder") {
    const relative = file.webkitRelativePath || file.name;
    return relative.split("/")[0] || file.name;
  }
  return file.name.replace(/\.zip$/i, "") || file.name;
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function isEffectiveImportLimits(value: ImportLimits) {
  const fields = [
    value.max_files,
    value.max_file_bytes,
    value.max_total_bytes,
    value.max_compression_ratio,
  ];
  return fields.every((item) => Number.isSafeInteger(item) && item > 0)
    && value.max_file_bytes <= value.max_total_bytes;
}

function normalizedChapters(chapters: ImportChapterPreview[]) {
  return chapters.map((chapter, order_index) => ({ ...chapter, order_index }));
}

function errorMessage(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback;
}

function isRevisionConflict(cause: unknown): cause is ApiError {
  return cause instanceof ApiError
    && (cause.code === "DRAFT_REVISION_CONFLICT" || cause.code === "revision_conflict")
    && Boolean(cause.current);
}

export interface BookImportWizardHandle {
  cancel: () => Promise<void>;
}

interface BookImportWizardProps {
  onComplete: (result: ImportCommitResponse) => void | Promise<void>;
  onCancel: () => void;
  beforeCommit?: () => boolean;
}

export const BookImportWizard = forwardRef<BookImportWizardHandle, BookImportWizardProps>(
  function BookImportWizard({ onComplete, onCancel, beforeCommit }, ref) {
    const folderInput = useRef<HTMLInputElement>(null);
    const zipInput = useRef<HTMLInputElement>(null);
    const commitStarted = useRef(false);
    const committed = useRef(false);
    const closeAfterUpload = useRef(false);
    const cleanupRequiredRef = useRef(false);
    const discardDialog = useRef<HTMLElement>(null);
    const discardTrigger = useRef<HTMLElement | null>(null);
    const discardWasOpen = useRef(false);
    const wizardContent = useRef<HTMLDivElement>(null);
    const [step, setStep] = useState(1);
    const [sourceKind, setSourceKind] = useState<ImportSourceKind | null>(null);
    const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
    const [draft, setDraft] = useState<ImportDraft | null>(null);
    const [projectTitle, setProjectTitle] = useState("");
    const [busy, setBusy] = useState<"upload" | "patch" | "commit" | "discard" | null>(null);
    const [error, setError] = useState("");
    const [discardOpen, setDiscardOpen] = useState(false);
    const [cleanupRequired, setCleanupRequired] = useState(false);
    const [importLimits, setImportLimits] = useState<ImportLimits | null>(null);
    const [importLimitsState, setImportLimitsState] = useState<"loading" | "ready" | "error">("loading");

    useEffect(() => {
      let active = true;
      void api.importLimits().then((limits) => {
        if (!isEffectiveImportLimits(limits)) throw new Error("invalid import limits");
        if (!active) return;
        setImportLimits(limits);
        setImportLimitsState("ready");
      }).catch(() => {
        if (!active) return;
        setImportLimits(null);
        setImportLimitsState("error");
      });
      return () => { active = false; };
    }, []);

    function chooseFolder(event: ChangeEvent<HTMLInputElement>) {
      const files = Array.from(event.target.files ?? []);
      setSourceKind("folder");
      setSelectedFiles(files);
      setError(files.length ? "" : "请选择包含 .md 或 .txt 的文件夹。");
    }

    function chooseZip(event: ChangeEvent<HTMLInputElement>) {
      const files = Array.from(event.target.files ?? []);
      setSourceKind("zip");
      setSelectedFiles([]);
      if (files.length !== 1) {
        setError(files.length > 1 ? "只能选择一个 ZIP 文件。" : "请选择一个 ZIP 文件。");
        return;
      }
      if (!files[0].name.toLocaleLowerCase().endsWith(".zip")) {
        setError("请选择 .zip 文件。");
        return;
      }
      setSelectedFiles(files);
      setError("");
    }

    async function upload(event: FormEvent) {
      event.preventDefault();
      if (cleanupRequiredRef.current) {
        setError("必须先清理上一份导入草稿，不能再次上传。");
        return;
      }
      if (!sourceKind || !selectedFiles.length) {
        setError("请先选择要导入的文件夹或 ZIP。");
        return;
      }
      if (sourceKind === "zip" && (selectedFiles.length !== 1 || !selectedFiles[0].name.toLocaleLowerCase().endsWith(".zip"))) {
        setError("ZIP 导入只能选择一个 .zip 文件。");
        return;
      }
      const body = new FormData();
      body.set("source_kind", sourceKind);
      body.set("display_name", sourceName(selectedFiles[0], sourceKind));
      for (const file of selectedFiles) {
        body.append("files", file);
        if (sourceKind === "folder") body.append("paths", file.webkitRelativePath || file.name);
      }
      setBusy("upload");
      setError("");
      try {
        const created = await api.createImportDraft(body);
        if (closeAfterUpload.current) {
          cleanupRequiredRef.current = true;
          setDraft(created);
          setProjectTitle(created.title);
          setCleanupRequired(true);
          setDiscardOpen(true);
          setBusy("discard");
          try {
            await api.discardImportDraft(created.draft_id);
            cleanupRequiredRef.current = false;
            closeAfterUpload.current = false;
            setCleanupRequired(false);
            setDiscardOpen(false);
            setDraft(null);
            onCancel();
          } catch (cause) {
            setError(`已收到导入草稿，但自动清理失败：${errorMessage(cause, "未知错误")}。必须重试放弃导入后才能关闭。`);
          }
          return;
        }
        setDraft(created);
        setProjectTitle(created.title);
        setStep(2);
      } catch (cause) {
        if (closeAfterUpload.current) onCancel();
        else setError(errorMessage(cause, "导入预览失败，请检查文件后重试。"));
      } finally {
        setBusy(null);
      }
    }

    async function savePatch(data: Omit<ImportDraftPatch, "revision">, nextStep: number) {
      if (!draft) return;
      setBusy("patch");
      setError("");
      try {
        const current = await api.patchImportDraft(draft.draft_id, {
          ...data,
          revision: draft.revision,
        });
        setDraft(current);
        setProjectTitle(current.title);
        setStep(nextStep);
      } catch (cause) {
        if (isRevisionConflict(cause)) {
          const current = cause.current as ImportDraft;
          setDraft(current);
          setProjectTitle(current.title);
          setError("导入草稿已在别处更新，已刷新为最新版本，请重新检查。");
        } else {
          setError(errorMessage(cause, "保存导入设置失败，请重试。"));
        }
      } finally {
        setBusy(null);
      }
    }

    function updateFile(index: number, changes: Partial<ImportFilePreview>) {
      if (!draft) return;
      setDraft({
        ...draft,
        files: draft.files.map((file, fileIndex) => fileIndex === index ? { ...file, ...changes } : file),
      });
    }

    function updateChapter(index: number, changes: Partial<ImportChapterPreview>) {
      if (!draft) return;
      setDraft({
        ...draft,
        chapters: draft.chapters.map((chapter, chapterIndex) => (
          chapterIndex === index ? { ...chapter, ...changes } : chapter
        )),
      });
    }

    function moveChapter(index: number, direction: -1 | 1) {
      if (!draft) return;
      const destination = index + direction;
      if (destination < 0 || destination >= draft.chapters.length) return;
      const chapters = [...draft.chapters];
      [chapters[index], chapters[destination]] = [chapters[destination], chapters[index]];
      setDraft({ ...draft, chapters: normalizedChapters(chapters) });
    }

    async function saveContinuation(event: FormEvent) {
      event.preventDefault();
      if (!draft) return;
      const completedIndex = draft.chapters.findIndex(
        (chapter) => chapter.draft_chapter_id === draft.continuation.completed_through_node_id,
      );
      const currentIndex = draft.chapters.findIndex(
        (chapter) => chapter.draft_chapter_id === draft.continuation.current_chapter_id,
      );
      if (completedIndex >= 0 && currentIndex >= 0 && currentIndex < completedIndex) {
        setError("当前未完成章节不能早于已完成章节。");
        return;
      }
      if (!draft.continuation.confirmed) {
        setError("请先确认续写边界。");
        return;
      }
      const continuation = {
        ...draft.continuation,
        source_document_ids: draft.files
          .filter((file) => file.selected && file.category === "task")
          .map((file) => file.audit_id),
      };
      await savePatch({ continuation, objective: continuation.objective }, 4);
    }

    async function commit(event: FormEvent) {
      event.preventDefault();
      if (!draft || commitStarted.current || busy) return;
      const title = projectTitle.trim();
      if (!title) {
        setError("请输入小说名称。");
        return;
      }
      if (!draft.continuation.confirmed) {
        setError("续写边界尚未由服务器确认，请返回上一步保存。");
        return;
      }
      if (beforeCommit && !beforeCommit()) return;
      commitStarted.current = true;
      setBusy("commit");
      setError("");
      try {
        const saved = await api.patchImportDraft(draft.draft_id, {
          revision: draft.revision,
          title,
        });
        setDraft(saved);
        setProjectTitle(saved.title);
        if (!saved.title.trim() || !saved.continuation.confirmed) {
          setError("服务器尚未确认项目名称或续写边界，请检查后重试。");
          commitStarted.current = false;
          return;
        }
        const result = await api.commitImportDraft(saved.draft_id, saved.revision);
        committed.current = true;
        await onComplete(result);
      } catch (cause) {
        if (isRevisionConflict(cause)) {
          const current = cause.current as ImportDraft;
          setDraft(current);
          setProjectTitle(current.title);
          setError("导入草稿已更新，已刷新为最新版本，请重新确认后提交。");
        } else {
          setError(errorMessage(cause, "创建导入项目失败，请重试。"));
        }
        commitStarted.current = false;
      } finally {
        setBusy(null);
      }
    }

    async function cancel() {
      if (committed.current) return;
      if (discardOpen || cleanupRequiredRef.current) return;
      discardTrigger.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      if (busy === "upload" && !draft) {
        closeAfterUpload.current = true;
        setError("上传正在安全结束，收到草稿后会先清理再关闭。");
        return;
      }
      if (!draft) {
        onCancel();
        return;
      }
      setDiscardOpen(true);
    }

    async function discard() {
      if (!draft || busy) return;
      setBusy("discard");
      setError("");
      try {
        await api.discardImportDraft(draft.draft_id);
        cleanupRequiredRef.current = false;
        closeAfterUpload.current = false;
        setCleanupRequired(false);
        setDiscardOpen(false);
        setDraft(null);
        onCancel();
      } catch (cause) {
        setError(errorMessage(cause, "放弃导入失败，请重试。"));
      } finally {
        setBusy(null);
      }
    }

    function closeDiscard() {
      if (cleanupRequiredRef.current || busy === "discard") return;
      setDiscardOpen(false);
      setError("");
    }

    function handleDiscardKeyDown(event: KeyboardEvent<HTMLElement>) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (!cleanupRequiredRef.current && busy !== "discard") closeDiscard();
        return;
      }
      if (event.key !== "Tab") return;
      event.stopPropagation();
      const nodes = [
        ...(discardDialog.current?.querySelectorAll<HTMLElement>(
          "button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href]",
        ) ?? []),
      ];
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (!first || !last) {
        event.preventDefault();
        discardDialog.current?.focus();
        return;
      }
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }

    useEffect(() => {
      const content = wizardContent.current;
      if (content) content.inert = discardOpen;
      if (!discardOpen) {
        if (discardWasOpen.current) {
          discardWasOpen.current = false;
          queueMicrotask(() => discardTrigger.current?.focus());
        }
        return;
      }
      discardWasOpen.current = true;
      const modalHeading = content?.closest(".editor-sheet")
        ?.querySelector<HTMLElement>(":scope > .sheet-heading");
      const headingWasInert = modalHeading?.inert ?? false;
      const headingAriaHidden = modalHeading?.getAttribute("aria-hidden");
      if (modalHeading) {
        modalHeading.inert = true;
        modalHeading.setAttribute("aria-hidden", "true");
      }
      return () => {
        if (content) content.inert = false;
        if (modalHeading) {
          modalHeading.inert = headingWasInert;
          if (headingAriaHidden == null) modalHeading.removeAttribute("aria-hidden");
          else modalHeading.setAttribute("aria-hidden", headingAriaHidden);
        }
      };
    }, [discardOpen]);

    useEffect(() => {
      if (!discardOpen) return;
      const first = discardDialog.current?.querySelector<HTMLElement>("button:not(:disabled)");
      (first ?? discardDialog.current)?.focus();
    }, [busy, cleanupRequired, discardOpen]);

    useImperativeHandle(ref, () => ({ cancel }));

    const selectedFilesCount = draft?.files.filter((file) => file.selected).length ?? 0;
    const selectedCategories = draft?.files.filter((file) => file.selected).reduce<Record<string, number>>(
      (counts, file) => ({ ...counts, [file.category]: (counts[file.category] ?? 0) + 1 }),
      {},
    ) ?? {};

    return (
      <section className="book-import-wizard" aria-labelledby="book-import-title" aria-busy={Boolean(busy)}>
        <div ref={wizardContent} className="book-import-workspace" aria-hidden={discardOpen ? "true" : undefined}>
          <header className="book-import-heading">
          <div>
            <span className="eyebrow">本地导入 · 原始文件会保留</span>
            <h2 id="book-import-title">导入已有小说</h2>
          </div>
          <span className="import-step-counter">第 {step}/4 步</span>
          <button type="button" className="text-action import-cancel-action" disabled={Boolean(busy)} onClick={() => void cancel()}>取消导入</button>
          </header>

          <div className="import-page-frame">
          <ol className="import-spine-steps" aria-label="导入进度">
            {steps.map((label, index) => (
              <li key={label} aria-current={step === index + 1 ? "step" : undefined}>
                <span aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
                <span>{label}</span>
              </li>
            ))}
          </ol>

          <div className="import-page-content">
            {error && <p role="alert" className="error-note">{error}</p>}

            {step === 1 && (
              <form aria-labelledby="import-source-heading" aria-busy={busy === "upload"} onSubmit={upload}>
                <header className="import-form-heading">
                  <h3 id="import-source-heading">选择导入来源</h3>
                  <p>.md 与 .txt 支持 UTF-8、UTF-8 BOM、GB18030；也可选择一个 ZIP。</p>
                </header>
                <section className="import-limit-card" aria-label="当前导入安全限制">
                  <h4>当前服务器限制</h4>
                  {importLimitsState !== "ready" && (
                    <div role="status" aria-label="导入限制状态" aria-live="polite" aria-atomic="true">
                      {importLimitsState === "loading" && <p>正在读取实际导入限制…</p>}
                      {importLimitsState === "error" && (
                        <p className="import-limit-error">暂时无法读取服务器的实际导入限制；仍可选择来源，上传时服务器会按实际限制校验。</p>
                      )}
                    </div>
                  )}
                  {importLimitsState === "ready" && importLimits && (
                    <dl>
                      <div><dt>文件数量</dt><dd>最多 {importLimits.max_files.toLocaleString()} 个文本文件</dd></div>
                      <div><dt>单文件</dt><dd>{formatBytes(importLimits.max_file_bytes)}</dd></div>
                      <div><dt>总展开量</dt><dd>{formatBytes(importLimits.max_total_bytes)}</dd></div>
                      <div><dt>ZIP 压缩比</dt><dd>{importLimits.max_compression_ratio}:1</dd></div>
                    </dl>
                  )}
                  <small>这些值来自当前服务器；本地配置只能调低，上传内容不能放宽。</small>
                </section>
                <div className="import-source-actions">
                  <input ref={folderInput} className="visually-hidden" aria-label="小说文件夹" type="file" accept=".md,.txt,text/markdown,text/plain" multiple {...({ webkitdirectory: "" } as object)} onChange={chooseFolder} />
                  <button type="button" className="secondary-action" disabled={Boolean(busy)} onClick={() => folderInput.current?.click()}>选择文件夹</button>
                  <input ref={zipInput} className="visually-hidden" aria-label="ZIP 文件" type="file" accept=".zip,application/zip" onChange={chooseZip} />
                  <button type="button" className="secondary-action" disabled={Boolean(busy)} onClick={() => zipInput.current?.click()}>选择 ZIP</button>
                </div>
                <p className="import-selection-status" role="status">
                  {selectedFiles.length ? `已选择 ${sourceKind === "zip" ? selectedFiles[0].name : `${selectedFiles.length} 个文件`}` : "尚未选择来源"}
                </p>
                <footer className="import-step-actions">
                  <button className="primary-action" type="submit" disabled={Boolean(busy) || !selectedFiles.length}>{busy === "upload" ? "正在检查文件…" : "下一步：检查章节"}</button>
                </footer>
              </form>
            )}

            {step === 2 && draft && (
              <form aria-labelledby="import-review-heading" aria-busy={busy === "patch"} onSubmit={(event) => {
                event.preventDefault();
                void savePatch({ files: draft.files, chapters: normalizedChapters(draft.chapters) }, 3);
              }}>
                <header className="import-form-heading">
                  <h3 id="import-review-heading">检查文件与章节</h3>
                  <p>自动分类只是一份建议。排除不会删除原文件，恢复后仍可导入。</p>
                </header>
                <div className="import-table-scroll" tabIndex={0} aria-label="导入文件检查表">
                  <table className="import-file-table">
                    <thead><tr><th>文件</th><th>分类</th><th>格式</th><th>状态</th></tr></thead>
                    <tbody>
                      {draft.files.map((file, index) => (
                        <tr key={file.audit_id || file.relative_path} className={file.selected ? undefined : "is-excluded"}>
                          <td>
                            <strong>{file.relative_path}</strong>
                            {(file.category === "task" || file.category === "other") && file.content_preview && (
                              <div className="import-text-preview"><span>文本预览</span><pre>{file.content_preview}</pre></div>
                            )}
                          </td>
                          <td>
                            <select aria-label={`${file.relative_path} 分类`} value={file.category} disabled={Boolean(busy) || !file.selected} onChange={(event) => updateFile(index, { category: event.target.value as ImportCategory })}>
                              {Object.entries(categoryLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                            </select>
                            <span className="file-category-label">{categoryLabels[file.category]}</span>
                          </td>
                          <td><span>{file.encoding.toLocaleUpperCase()} · {formatBytes(file.size_bytes)}</span></td>
                          <td>
                            {file.warning && <span className="import-warning">{file.warning}</span>}
                            <button type="button" className="text-action" aria-label={`${file.selected ? "排除" : "恢复"} ${file.relative_path}`} disabled={Boolean(busy)} onClick={() => updateFile(index, { selected: !file.selected })}>{file.selected ? "排除" : "恢复"}</button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <section className="import-chapter-review" aria-labelledby="chapter-review-title">
                  <header><h4 id="chapter-review-title">章节顺序</h4><span>{draft.chapters.length} 章</span></header>
                  {draft.chapters.length === 0 && <p>没有识别到正文章节；所选资料仍会作为可检索来源导入。</p>}
                  <ol>
                    {draft.chapters.map((chapter, index) => (
                      <li key={chapter.draft_chapter_id || `${chapter.relative_path}-${index}`}>
                        <span className="chapter-order" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
                        <label>章节名称<input aria-label={`章节名称 ${chapter.title}`} value={chapter.title} required disabled={Boolean(busy)} onChange={(event) => updateChapter(index, { title: event.target.value })} /></label>
                        <span className="chapter-source">{chapter.relative_path}</span>
                        <div className="chapter-order-actions">
                          <button type="button" aria-label={`上移 ${chapter.title}`} disabled={Boolean(busy) || index === 0} onClick={() => moveChapter(index, -1)}>上移</button>
                          <button type="button" aria-label={`下移 ${chapter.title}`} disabled={Boolean(busy) || index === draft.chapters.length - 1} onClick={() => moveChapter(index, 1)}>下移</button>
                        </div>
                      </li>
                    ))}
                  </ol>
                </section>
                <footer className="import-step-actions">
                  <button type="button" disabled={Boolean(busy)} onClick={() => setStep(1)}>返回上一步</button>
                  <button className="primary-action" type="submit" disabled={Boolean(busy) || !draft.files.some((file) => file.selected)}>{busy === "patch" ? "正在保存…" : "下一步：确认续写点"}</button>
                </footer>
              </form>
            )}

            {step === 3 && draft && (
              <form aria-labelledby="import-continuation-heading" aria-busy={busy === "patch"} onSubmit={(event) => void saveContinuation(event)}>
                <header className="import-form-heading"><h3 id="import-continuation-heading">确认续写点</h3><p>这三项会成为 Agent 打开项目时的明确交接边界。</p></header>
                {draft.files.some((file) => file.selected && file.category === "task") && (
                  <section className="import-task-notes" aria-labelledby="task-notes-title">
                    <h4 id="task-notes-title">任务资料预览</h4>
                    {draft.files.filter((file) => file.selected && file.category === "task").map((file) => <article key={file.audit_id}><strong>{file.relative_path}</strong><pre>{file.content_preview}</pre></article>)}
                  </section>
                )}
                <div className="import-continuation-grid">
                  <label>已完成到<select value={draft.continuation.completed_through_node_id ?? ""} disabled={Boolean(busy)} onChange={(event) => setDraft({ ...draft, continuation: { ...draft.continuation, completed_through_node_id: event.target.value || null, confirmed: false } })}>
                    <option value="">未指定</option>{draft.chapters.map((chapter) => <option value={chapter.draft_chapter_id} key={chapter.draft_chapter_id}>{chapter.title}</option>)}
                  </select></label>
                  <label>当前未完成章节<select value={draft.continuation.current_chapter_id ?? ""} disabled={Boolean(busy)} onChange={(event) => setDraft({ ...draft, continuation: { ...draft.continuation, current_chapter_id: event.target.value || null, confirmed: false } })}>
                    <option value="">没有未完成章节</option>{draft.chapters.map((chapter) => <option value={chapter.draft_chapter_id} key={chapter.draft_chapter_id}>{chapter.title}</option>)}
                  </select></label>
                </div>
                <label>下一步写作目标<textarea value={draft.continuation.objective} disabled={Boolean(busy)} onChange={(event) => setDraft({ ...draft, continuation: { ...draft.continuation, objective: event.target.value, confirmed: false } })} /></label>
                <label className="import-confirm-boundary"><input aria-label="我已确认续写边界" type="checkbox" checked={draft.continuation.confirmed} disabled={Boolean(busy)} onChange={(event) => setDraft({ ...draft, continuation: { ...draft.continuation, confirmed: event.target.checked } })} /><span><strong>我已确认续写边界</strong><small>Agent 将从这里继续，不会把任务资料当作系统指令。</small></span></label>
                <footer className="import-step-actions">
                  <button type="button" disabled={Boolean(busy)} onClick={() => setStep(2)}>返回上一步</button>
                  <button className="primary-action" type="submit" disabled={Boolean(busy) || !draft.continuation.confirmed}>{busy === "patch" ? "正在保存…" : "下一步：创建项目"}</button>
                </footer>
              </form>
            )}

            {step === 4 && draft && (
              <form aria-labelledby="import-create-heading" aria-busy={busy === "commit"} onSubmit={(event) => void commit(event)}>
                <header className="import-form-heading"><h3 id="import-create-heading">创建导入项目</h3><p>基础导入不依赖 AI；分析暂不可用也不会阻止你打开和继续编辑。</p></header>
                <label>小说名称<input value={projectTitle} required maxLength={240} disabled={Boolean(busy)} onChange={(event) => setProjectTitle(event.target.value)} /></label>
                <section className="import-commit-summary" aria-labelledby="import-summary-title">
                  <h4 id="import-summary-title">导入摘要</h4>
                  <dl>
                    <div><dt>文件</dt><dd>{selectedFilesCount} 份</dd></div><div><dt>章节</dt><dd>{draft.chapters.length} 个章节</dd></div>
                    {Object.entries(selectedCategories).map(([category, count]) => <div key={category}><dt>{categoryLabels[category as ImportCategory]}</dt><dd>{count}</dd></div>)}
                    <div><dt>完成边界</dt><dd>{draft.chapters.find((chapter) => chapter.draft_chapter_id === draft.continuation.completed_through_node_id)?.title ?? "未指定"}</dd></div>
                    <div><dt>当前章节</dt><dd>{draft.chapters.find((chapter) => chapter.draft_chapter_id === draft.continuation.current_chapter_id)?.title ?? "无"}</dd></div>
                  </dl>
                  {draft.continuation.objective && <p><strong>续写目标</strong>{draft.continuation.objective}</p>}
                </section>
                <footer className="import-step-actions">
                  <button type="button" disabled={Boolean(busy)} onClick={() => setStep(3)}>返回上一步</button>
                  <button className="primary-action" type="submit" disabled={Boolean(busy) || !projectTitle.trim() || !draft.title.trim() || !draft.continuation.confirmed}>{busy === "commit" ? "正在创建项目…" : "创建并导入小说"}</button>
                </footer>
              </form>
            )}
          </div>
          </div>
        </div>

        {discardOpen && (
          <section ref={discardDialog} className="import-discard-confirm" role="alertdialog" aria-modal="true" aria-labelledby="import-discard-title" tabIndex={-1} onKeyDown={handleDiscardKeyDown}>
            <h3 id="import-discard-title">放弃这次导入？</h3><p>上传的导入草稿会被清理，尚未创建小说项目。</p>
            {error && <p role="alert" className="error-note">{error}</p>}
            <div><button type="button" disabled={cleanupRequired || busy === "discard"} onClick={closeDiscard}>继续编辑</button><button type="button" className="danger-action" disabled={busy === "discard"} onClick={() => void discard()}>{busy === "discard" ? "正在放弃…" : "放弃导入"}</button></div>
          </section>
        )}
      </section>
    );
  },
);
