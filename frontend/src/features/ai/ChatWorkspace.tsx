import { useEffect, useId, useRef, useState } from "react";
import type { AIJob, JobSummary } from "../../lib/api";
import { Icon } from "../../components/Icon";
import { ContextPreview } from "./ContextPreview";
import { JobStatus } from './JobStatus';
import { JobPreview } from './JobPreview';
import { FeedbackPanel, type FeedbackPayload } from "../feedback/FeedbackPanel";
import { inputBudgetValue, MAX_INPUT_BUDGET } from './inputBudget';
import { loadLocalDrafts, saveLocalDraft, useLocalDraft, type LocalDraft } from '../../lib/draftStore';
import { DraftRecoveryNotice } from '../../components/LocalDraftRecovery';
import { DiagnosticError } from '../../components/DiagnosticError';
import { PromptOptimizer } from './PromptOptimizer';
import { PROMPT_MAX_LENGTH } from './promptOptimizerEngine';

const actions = [
  ["chat", "讨论", "一起讨论这个故事的下一步。"],
  ["plan", "规划", "根据本章目标规划场景与转折。"],
  ["continue", "续写", "衔接现有正文继续写作。"],
  ["rewrite", "改写", "保留核心事件，优化本章叙述与节奏。"],
  ["review", "检查", "检查本章与前文、人物设定是否一致。"],
] as const;

// Component navigation shares this page's draft; a new browser document must ask to recover.
type NavigationDraft = { message: string; task: string; draft: LocalDraft | null; persisted: boolean };
const navigationDrafts = new Map<string, NavigationDraft>();
if (typeof window !== 'undefined') window.addEventListener('pagehide', () => navigationDrafts.clear());
function readNavigationDraft(scope?: string) {
  const value = scope ? navigationDrafts.get(scope) : undefined;
  if (value?.persisted && value.draft) {
    const stored = loadLocalDrafts(value.draft.projectId, value.draft.chapterId, 'chat', value.draft.conversationId);
    if (!stored.unavailable && !stored.drafts.some(draft => draft.id === value.draft!.id)) return undefined;
  }
  return value;
}

function outputText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value !== "object") return String(value);
  if (Array.isArray(value)) return value.map(outputText).join("\n\n");
  const labels: Record<string, string> = {
    scenes: "场景规划",
    title: "场景",
    goal: "目标",
    obstacle: "阻力",
    turn: "转折",
    state_change: "变化",
    summary: "总结",
    issues: "发现的问题",
    result: "结果",
  };
  return Object.entries(value)
    .map(
      ([key, item]) =>
        `${labels[key] ?? key}：${Array.isArray(item) && !item.length ? "无" : outputText(item)}`,
    )
    .join("\n");
}

export function ChatWorkspace({
  chapterTitle,
  hasChapter,
  jobs,
  running,
  disabledReason,
  draftKey,
  onSend,
  beforeSend,
  onAccept,
  onOpenManuscript,
  onSettings,
  onFeedback,
  onCancel,
  onResume,
  onReplace,
  replaceDisabled,
  sourceUnavailable = false,
  projectId,
  chapterId,
  conversationId,
  appliedStyles,
  selectedJobId,
  selectedJob,
  onSelectJob,
  onLoadOlder,
  loadingOlder,
  detailLoading,
  detailError,
  onRetryDetail,
  inputBudget,
  onInputBudgetChange,
  inputBudgetAutomatic,
  onFollowModel,
}: {
  chapterTitle: string;
  hasChapter: boolean;
  jobs: JobSummary[];
  projectId?: string;
  chapterId?: string;
  conversationId?: string;
  appliedStyles?: string[];
  selectedJobId?: string;
  selectedJob?: AIJob;
  onSelectJob?: (id: string) => void;
  onLoadOlder?: () => Promise<unknown>;
  loadingOlder?: boolean;
  detailLoading?: boolean;
  detailError?: unknown;
  onRetryDetail?: () => void;
  inputBudget?: string;
  onInputBudgetChange?: (value: string) => void;
  inputBudgetAutomatic?: boolean;
  onFollowModel?: () => void;
  running: boolean;
  disabledReason?: string;
  draftKey?: string;
  onSend: (task: string, instructions: string) => Promise<void>;
  beforeSend?: (task: string) => boolean | Promise<boolean>;
  onAccept: (id: string) => Promise<void>;
  onOpenManuscript: () => void;
  onSettings?: () => void;
  onFeedback?: (job: AIJob, payload: FeedbackPayload) => Promise<void>;
  onCancel?: (job: JobSummary) => Promise<unknown>;
  onResume?: (job: JobSummary, confirm: boolean) => Promise<unknown>;
  onReplace?: (job: JobSummary) => Promise<unknown>;
  replaceDisabled?: boolean;
  sourceUnavailable?: boolean;
}) {
  const navigationScope = projectId ? JSON.stringify([projectId, chapterId ?? null, conversationId ?? null]) : draftKey;
  const [navigationDraft] = useState(() => readNavigationDraft(navigationScope));
  const [message, setMessage] = useState(navigationDraft?.message ?? ''),
    [task, setTask] = useState(navigationDraft?.task ?? 'chat'),
    [error, setError] = useState<unknown>(null);
  const inputLengthId = useId();
  const messageTooLong = message.length > PROMPT_MAX_LENGTH;
  const recovery = useLocalDraft({ projectId, chapterId: chapterId ?? null, conversationId, title: chapterTitle,
    kind: 'chat', initialDraft: navigationDraft?.draft ?? undefined, values: { message, task }, dirty: !!message });
  const navigationState = useRef({ message, task, snapshot: recovery.snapshot, persisted: recovery.status === 'saved' });
  navigationState.current = { message, task, snapshot: recovery.snapshot, persisted: recovery.status === 'saved' };
  useEffect(() => {
    if (!navigationScope) return;
    navigationDrafts.delete(navigationScope);
    return () => {
      const current = navigationState.current;
      if (current.message) navigationDrafts.set(navigationScope, { message: current.message, task: current.task, draft: current.snapshot(), persisted: current.persisted });
      else navigationDrafts.delete(navigationScope);
    };
  }, [navigationScope]);
  const tail = useRef<HTMLDivElement>(null);
  const action = actions.find((item) => item[0] === task)!;
  useEffect(() => {
    if (!draftKey || !projectId || conversationId) return;
    try {
      const legacy = sessionStorage.getItem(draftKey);
      if (!legacy) return;
      const existing = loadLocalDrafts(projectId, chapterId ?? null, 'chat').drafts.some(d => d.values.message === legacy);
      if (existing || saveLocalDraft({ version: 1, id: crypto.randomUUID(), projectId, chapterId: chapterId ?? null,
        kind: 'chat', baseRevision: null, updatedAt: new Date().toISOString(), values: { message: legacy, task: 'chat' } })) sessionStorage.removeItem(draftKey);
    } catch {
      /* Keep the in-memory draft when browser storage is unavailable. */
    }
  }, [draftKey, projectId, chapterId, conversationId]);
  useEffect(() => {
    if (jobs.length || running) tail.current?.scrollIntoView?.({ block: "end" });
  }, [jobs.length, running]);
  async function send() {
    if (running || disabledReason || messageTooLong || (!hasChapter && task !== "chat")) return;
    setError("");
    const submitted = message;
    try {
      if (inputBudget !== undefined) inputBudgetValue(inputBudget);
      if (beforeSend && !(await beforeSend(task))) return;
      await onSend(task, message.trim() || action[2]);
      setMessage(current => current === submitted ? '' : current);
    } catch (e) {
      setError(e instanceof Error ? e : Error('发送失败，请重试。'));
    }
  }
  return (
    <section className="chat-workspace" aria-label="AI 创作对话">
      <header className="chat-heading" role="group">
        <div>
          <span className="subtle">正在创作</span>
          <h1>{chapterTitle}</h1>
        </div>
        <button onClick={onOpenManuscript}>
          <Icon name="panel" size={16} />
          查看正文
        </button>
      </header>
      <DraftRecoveryNotice drafts={recovery.candidates} status={recovery.status} onDiscard={recovery.discard}
        onRestore={saved => {
          if (message && !window.confirm('用恢复稿替换当前未发送消息？当前草稿仍保留在本机恢复箱。')) return;
          recovery.adopt(saved); setMessage(String(saved.values.message)); setTask(String(saved.values.task));
        }} />
      {appliedStyles !== undefined && <p className="subtle">本次采用文风：{appliedStyles.length ? appliedStyles.join('、') : '无文风来源记录'}</p>}
      <div className={`chat-scroll ${jobs.length ? "" : "is-empty"}`}>
        {onLoadOlder && <button type="button" disabled={loadingOlder} onClick={() => {
          setError(''); void onLoadOlder().catch(e => setError(e));
        }}>{loadingOlder ? '正在加载…' : '加载更早对话'}</button>}
        {!jobs.length && (
          <div className="chat-welcome">
            <div className="chat-symbol">
              <Icon name="write" size={28} />
            </div>
            <h2>
              下一段故事，
              <br />
              从你的想法开始。
            </h2>
            <p>聊情节、推敲人物，或直接交代这一章怎么写。</p>
            <div className="starter-prompts">
              {[
                "一起梳理下一章的情节",
                "让人物的动机更可信",
                "帮我找到一个有张力的开场",
              ].map((prompt) => (
                <button
                  key={prompt}
                  onClick={() => {
                    setTask("chat");
                    setMessage(prompt);
                  }}
                >
                  {prompt}
                  <Icon name="arrow" size={15} />
                </button>
              ))}
            </div>
            {onSettings && (
              <button className="text-action" onClick={onSettings}>
                配置我的 AI 模型
              </button>
            )}
          </div>
        )}
        {jobs.map((job) => {
          const detail = selectedJob?.id === job.id && selectedJobId === job.id &&
            selectedJob.status === job.status && selectedJob.control_revision === job.control_revision
            ? selectedJob : undefined;
          return (
          <article className="chat-turn" key={job.id}>
            <div className="author-message">
              <span className="message-label">你</span>
              <p>
                {(detail?.instructions ?? job.instructions) ||
                  actions.find((item) => item[0] === job.task_type)?.[2] ||
                  "创作请求"}
              </p>
            </div>
            <div className="assistant-message">
              <span className="message-label">
                <Icon name="spark" size={15} />
                创作助手
                <span>
                  {actions.find((item) => item[0] === job.task_type)?.[1] ??
                    "写作"}
                </span>
              </span>
              {job.inherited && <p className="subtle">分支继承记录 · 只读</p>}
              {job.status !== 'succeeded' && !job.inherited && <JobStatus job={job}
                sourceUnavailable={sourceUnavailable}
                onCancel={onCancel ? () => onCancel(job) : undefined}
                onResume={onResume ? confirm => onResume(job, confirm) : undefined}
                onReplace={onReplace ? () => onReplace(job) : undefined}
                replaceDisabled={running || replaceDisabled} />}
              {job.replaces_job_id && <p className="subtle">原任务：{job.replaces_job_id}</p>}
              {projectId && selectedJobId === job.id && job.status === 'running' &&
                <JobPreview key={`${projectId}:${job.id}:${job.control_revision}`} projectId={projectId} job={job} />}
              {job.status === "failed" ? (
                <p role="alert" className="error-note">
                  {job.error_message || "生成失败，可重新发送。"}
                </p>
              ) : (
                <p className="assistant-prose">
                  {detail?.result.reply ||
                    detail?.result.candidate_text ||
                    outputText(detail?.result.output) || job.preview}
                </p>
              )}
              {!detail && onSelectJob && <button type="button" aria-label={`查看完整回复 ${job.id}`}
                disabled={detailLoading && selectedJobId === job.id} onClick={() => onSelectJob(job.id)}>
                {detailLoading && selectedJobId === job.id ? '正在读取完整回复…' : '查看完整回复与参考'}
              </button>}
              {selectedJobId === job.id && !!detailError && <div className="error-note">
                <DiagnosticError error={detailError} /><button type="button" onClick={onRetryDetail}>重试读取</button>
              </div>}
              {!job.inherited && detail?.status === 'succeeded' && detail.result.candidate_text && (
                <div className="candidate-actions">
                  <span>
                    {job.accepted_version_id
                      ? "已写入正文并保存版本"
                      : "候选正文 · 等待你确认"}
                  </span>
                  <button
                    disabled={running || sourceUnavailable || !!job.accepted_version_id}
                    onClick={async () => {
                      setError("");
                      try {
                        await onAccept(job.id);
                      } catch (e) {
                        setError(e);
                      }
                    }}
                  >
                    {job.accepted_version_id ? "已采用" : "写入正文"}
                  </button>
                </div>
              )}
              {!!(
                detail?.context_snapshot?.fragments?.length ||
                detail?.context_snapshot?.dropped_source_ids?.length
              ) && (
                <details className="chat-references">
                  <summary>
                    检索到 {detail!.context_snapshot.fragments?.length ?? 0} 项参考
                  </summary>
                  <ContextPreview context={detail!.context_snapshot} />
                </details>
              )}
              {!job.inherited && onFeedback && detail && job.status === "succeeded" && (
                <details className="chat-references">
                  <summary>评价这次回复</summary>
                  <FeedbackPanel
                    originalText={
                      detail.result.reply || detail.result.candidate_text || outputText(detail.result.output)
                    }
                    onSubmit={(payload) => onFeedback(detail, payload)}
                  />
                </details>
              )}
            </div>
          </article>
        ); })}
        {running && (
          <p className="chat-progress" role="status">
            <span />
            正在提交请求…
          </p>
        )}
        <div ref={tail} />
      </div>
      <form
        className="chat-composer"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <div className="composer-toolbar">
        <div className="composer-actions" aria-label="写作操作">
          {actions.map(([id, label]) => (
            <button
              type="button"
              key={id}
              aria-pressed={task === id}
              disabled={running || (!hasChapter && id !== "chat")}
              onClick={() => setTask(id)}
            >
              {label}
            </button>
          ))}
        </div>
          {onInputBudgetChange && <label className="composer-budget" title="新任务默认使用模型容量减去输出预留，可填写较小的省费上限。发送时仅本地估算首阶段必要输入，不调用模型；后续阶段仍可能超限。">
            输入预算
            <input aria-label="输入预算 Token" type="number" min={256} max={MAX_INPUT_BUDGET} step={1}
              required value={inputBudget} onChange={event => onInputBudgetChange(event.target.value)} />
            <span>{inputBudgetAutomatic ? 'Token · 自动' : 'Token · 省费上限'}</span>
          </label>}
          {onFollowModel && <button type="button" className="text-action" aria-pressed={inputBudgetAutomatic}
            onClick={onFollowModel}>跟随模型</button>}
        </div>
        <textarea
          aria-label="给 AI 的消息"
          placeholder={task === "chat" ? "告诉 AI，你想怎么写…" : action[2]}
          value={message}
          aria-invalid={messageTooLong || undefined}
          aria-describedby={messageTooLong ? inputLengthId : undefined}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {messageTooLong && <p id={inputLengthId} role="status" className="error-note">
          输入超过 16,000 字符，已完整保留。请缩短后再发送或应用提示词优化。
        </p>}
        <PromptOptimizer key={navigationScope ?? 'composer'} value={message} task={task}
          hasChapter={hasChapter} chapterTitle={chapterTitle} disabled={running}
          onReplace={(expected, replacement) => {
            if (running || navigationState.current.message !== expected || replacement.length > PROMPT_MAX_LENGTH) return false;
            setMessage(replacement);
            return true;
          }} />
        <footer>
          <span>
            {hasChapter
              ? "结合本章与项目资料 · 写入前由你确认"
              : "可以先讨论故事，建立章节后开始写作"}
          </span>
          <button
            className="send-message"
            aria-label="发送消息"
            disabled={running || !!disabledReason || messageTooLong || (!hasChapter && task !== "chat")}
          >
            <Icon name="arrow" size={19} />
          </button>
        </footer>
        {disabledReason && (
          <p role="status" className="subtle">{disabledReason}</p>
        )}
        {!!error && <DiagnosticError error={error} />}
      </form>
    </section>
  );
}
