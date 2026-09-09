import { useEffect, useRef, useState } from "react";

import type { ChapterDocument } from "../../lib/types";

function contractLines(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join("\n") : String(value ?? "");
}

function normalizedForbidden(value: unknown): string[] {
  return contractLines(value).split(/\n|，|,/).map((item) => item.trim()).filter(Boolean);
}

interface ChapterWorkspaceProps {
  title: string;
  document: ChapterDocument;
  saving: boolean;
  sourceUnavailable?: boolean;
  onSave: (document: {
    content: string;
    contract: Record<string, unknown>;
    revision?: number;
  }) => Promise<ChapterDocument | void>;
  onComplete?: (document: {
    content: string;
    contract: Record<string, unknown>;
    revision?: number;
  }) => Promise<ChapterDocument | void>;
  onDirtyChange?: (dirty: boolean) => void;
  onDraftChange?: (draft: string) => void;
  replacement?: { document: ChapterDocument; draft: string };
}

export function ChapterWorkspace({
  title,
  document,
  saving,
  sourceUnavailable = false,
  onSave,
  onComplete,
  onDirtyChange,
  onDraftChange,
  replacement,
}: ChapterWorkspaceProps) {
  const [error, setError] = useState("");
  const [completing, setCompleting] = useState(false);
  const [baseline, setBaseline] = useState(document);
  const [content, setContent] = useState(document.content);
  const [purpose, setPurpose] = useState(
    String(document.contract.purpose ?? ""),
  );
  const [forbidden, setForbidden] = useState(contractLines(document.contract.forbidden_revelations));
  const appliedReplacement = useRef(replacement);
  const savePending = useRef(false);
  const draft = JSON.stringify([content, purpose, forbidden]);
  const forbiddenValue = JSON.stringify(normalizedForbidden(forbidden));

  const dirty =
    content !== baseline.content ||
    purpose !== String(baseline.contract.purpose ?? "") ||
    forbiddenValue !== JSON.stringify(normalizedForbidden(baseline.contract.forbidden_revelations));
  useEffect(() => {
    if (replacement && replacement !== appliedReplacement.current) {
      appliedReplacement.current = replacement;
      setBaseline(replacement.document);
      if (draft === replacement.draft) {
        setContent(replacement.document.content);
        setPurpose(String(replacement.document.contract.purpose ?? ""));
        setForbidden(contractLines(replacement.document.contract.forbidden_revelations));
      }
      return;
    }
    // A saved response can arrive before the parent query refreshes.
    if (
      document.revision !== undefined && baseline.revision !== undefined &&
      document.revision < baseline.revision
    ) return;
    const nextPurpose = String(document.contract.purpose ?? "");
    const nextForbidden = contractLines(document.contract.forbidden_revelations);
    const matchesDraft =
      document.content === content &&
      nextPurpose === purpose &&
      JSON.stringify(normalizedForbidden(nextForbidden)) === forbiddenValue;
    if (!dirty || matchesDraft) {
      if (!matchesDraft) {
        setContent(document.content);
        setPurpose(nextPurpose);
        setForbidden(nextForbidden);
      }
      setBaseline(document);
    }
  }, [document, baseline.revision, content, purpose, forbiddenValue, dirty, draft, replacement]);
  useEffect(() => {
    onDraftChange?.(draft);
  }, [draft, onDraftChange]);
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  async function save(complete = false) {
    if (saving || sourceUnavailable || savePending.current) return;
    savePending.current = true;
    setError("");
    setCompleting(complete);
    try {
      const saved = await (complete && onComplete ? onComplete : onSave)({
        content,
        revision: baseline.revision,
        contract: {
          ...baseline.contract,
          purpose,
          forbidden_revelations: normalizedForbidden(forbidden),
        },
      });
      if (saved) setBaseline(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      savePending.current = false;
      setCompleting(false);
    }
  }

  return (
    <section className="chapter-workspace">
      <header role="group" className="chapter-heading">
        <div>
          <span className="eyebrow">章节工作台</span>
          <h1>{title}</h1>
        </div>
        <div className="word-meter">
          <strong>{content.replace(/\s/g, "").length}</strong>
          <small>字</small>
        </div>
      </header>
      <div className="contract-strip">
        <label>
          本章目的
          <input
            aria-label="本章目的"
            disabled={completing && !sourceUnavailable}
            value={purpose}
            onChange={(event) => setPurpose(event.target.value)}
            placeholder="这一章必须改变什么？"
          />
        </label>
        <label>
          禁止提前揭示
          <textarea
            aria-label="禁止提前揭示"
            disabled={completing && !sourceUnavailable}
            value={forbidden}
            onChange={(event) => setForbidden(event.target.value)}
            placeholder="每行一项"
          />
        </label>
      </div>
      <textarea
        className="manuscript-editor"
        aria-label="章节正文"
        disabled={completing && !sourceUnavailable}
        value={content}
        onChange={(event) => setContent(event.target.value)}
        spellCheck={false}
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes("text/plain"))
            event.preventDefault();
        }}
        onDrop={(event) => {
          const token = event.dataTransfer.getData("text/plain");
          if (/^\[\[ref:[a-z_]+:[A-Za-z0-9_-]+\]\]$/.test(token)) {
            event.preventDefault();
            const start = event.currentTarget.selectionStart;
            const end = event.currentTarget.selectionEnd;
            setContent(content.slice(0, start) + token + content.slice(end));
          }
        }}
        onKeyDown={(event) => {
          if (
            (event.metaKey || event.ctrlKey) &&
            event.key.toLowerCase() === "s"
          ) {
            event.preventDefault();
            if (!saving) void save();
          }
        }}
      />
      {dirty && document.revision !== undefined && baseline.revision !== undefined &&
        document.revision > baseline.revision && (
        <p className="error-note" role="alert">
          其他窗口已更新本章。你的草稿已保留，请复制后重新加载，避免覆盖。
        </p>
      )}
      {error && (
        <p role="alert" className="error-note">
          {error}
        </p>
      )}
      <footer className="editor-footer">
        <span>
          {dirty
            ? "有未保存修改"
            : document.current_version_id
              ? "基于已保存版本编辑"
              : "尚未建立永久版本"}
        </span>
        <div>
          <button type="button" disabled={saving || sourceUnavailable} onClick={() => void save()}>
            {saving ? "保存中…" : "保存工作副本"}
          </button>
          {onComplete && (
            <button
              className="primary-action"
              type="button"
              disabled={saving || sourceUnavailable}
              onClick={() => void save(true)}
            >
              {document.status === "summary_pending"
                ? "重试章节总结"
                : "完成本章"}
            </button>
          )}
        </div>
      </footer>
    </section>
  );
}
