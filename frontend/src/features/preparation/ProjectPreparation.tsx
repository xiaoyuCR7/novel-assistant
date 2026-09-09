import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, projectApi } from "../../lib/api";
import type {
  PreparationQuestion,
  PreparationQuestionCommand,
  ProjectPreparationState,
} from "../../lib/types";

type PreparationClient = Pick<
  ReturnType<typeof projectApi>,
  | "preparation"
  | "initializePreparation"
  | "answerPreparation"
  | "skipPreparation"
  | "resumePreparation"
  | "acknowledgePreparationImpact"
  | "analyzePreparation"
  | "followUpPreparation"
  | "job"
  | "cancelJob"
  | "resumeJob"
>;

function initialAnswer(question: PreparationQuestion): string {
  return Array.isArray(question.answer) ? question.answer.join("\n") : question.answer ?? "";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请稍后重试。";
}

function requestKey(kind: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `preparation-${kind}-${suffix}`;
}

function isPreparationState(value: unknown, projectId: string): value is ProjectPreparationState {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const candidate = value as Partial<ProjectPreparationState>;
  return candidate.project_id === projectId
    && typeof candidate.revision === "number"
    && Array.isArray(candidate.questions);
}

const questionStatusLabels: Record<PreparationQuestion["status"], string> = {
  open: "待回答",
  answered: "已回答",
  deferred: "稍后回答",
  not_applicable: "不适用",
};

export function ProjectPreparation({
  projectId,
  client,
  compact = false,
  onOpenFull,
}: {
  projectId: string;
  client: PreparationClient;
  compact?: boolean;
  onOpenFull?: () => void;
}) {
  const queryClient = useQueryClient();
  const queryKey = useMemo(() => ["preparation", projectId] as const, [projectId]);
  const preparation = useQuery({ queryKey, queryFn: client.preparation });
  const [questionIndex, setQuestionIndex] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const state = preparation.data;
  const trackedJobId = jobId ?? state?.actionable_job_id ?? null;

  const job = useQuery({
    queryKey: ["preparation-job", projectId, trackedJobId],
    queryFn: () => client.job(trackedJobId!),
    enabled: Boolean(trackedJobId),
    refetchInterval: (query) =>
      ["succeeded", "failed", "cancelled", "recovery_required"].includes(
        String(query.state.data?.status),
      )
        ? false
        : 1000,
  });
  useEffect(() => {
    if (job.data?.status === "succeeded" || job.data?.status === "cancelled") {
      setJobId((current) => current === job.data?.id ? null : current);
      void queryClient.invalidateQueries({ queryKey });
    }
  }, [job.data?.status, queryClient, queryKey]);

  const currentQuestion = useMemo(() => {
    if (!state?.questions.length) return undefined;
    return state.questions[Math.min(questionIndex, state.questions.length - 1)];
  }, [questionIndex, state?.questions]);
  const currentDraft = currentQuestion
    ? drafts[currentQuestion.id] ?? initialAnswer(currentQuestion)
    : "";

  async function run(update: () => Promise<ProjectPreparationState>, clearId?: string) {
    setFailure("");
    try {
      const next = await update();
      queryClient.setQueryData(queryKey, next);
      if (clearId) {
        setDrafts((values) => {
          const copy = { ...values };
          delete copy[clearId];
          return copy;
        });
      }
    } catch (error) {
      if (
        error instanceof ApiError
        && error.code === "revision_conflict"
        && isPreparationState(error.current, projectId)
      ) {
        queryClient.setQueryData(queryKey, error.current);
      }
      setFailure(errorMessage(error));
    }
  }

  async function mutateQuestion(command: Omit<PreparationQuestionCommand, "revision">) {
    if (!state || !currentQuestion) return;
    await run(
      () =>
        client.answerPreparation(currentQuestion.id, {
          revision: state.revision,
          ...command,
        }),
      command.action === "answer" ? currentQuestion.id : undefined,
    );
  }

  async function submitAnswer() {
    if (!currentQuestion || !currentDraft.trim()) return;
    const answer =
      currentQuestion.answer_format === "ordered_list"
        ? currentDraft.split("\n").map((item) => item.trim()).filter(Boolean)
        : currentDraft.trim();
    await mutateQuestion({ action: "answer", answer });
  }

  async function startAi(kind: "analysis" | "followup") {
    setFailure("");
    try {
      const receipt = await (kind === "analysis"
        ? client.analyzePreparation(requestKey(kind))
        : client.followUpPreparation(requestKey(kind)));
      setJobId(receipt.id);
    } catch (error) {
      if (error instanceof ApiError && error.code === "PREPARATION_JOB_ACTIVE" && error.jobId) {
        setJobId(error.jobId);
      }
      setFailure(errorMessage(error));
    }
  }

  async function resumeAi() {
    if (!trackedJobId || !job.data) return;
    const confirmUnknown = Boolean(
      job.data.replacement_requires_confirmation || job.data.recovery_reason === "result_unknown",
    );
    if (
      confirmUnknown
      && !window.confirm("上次请求的结果未知，恢复可能产生一次新的模型调用。确认继续吗？")
    ) return;
    setFailure("");
    try {
      const resumed = await client.resumeJob(
        trackedJobId,
        {
          expected_control_revision: job.data.control_revision ?? 0,
          confirm_unknown: confirmUnknown,
        },
        requestKey("resume"),
      );
      setJobId(resumed.id);
      queryClient.setQueryData(["preparation-job", projectId, resumed.id], resumed);
    } catch (error) {
      setFailure(errorMessage(error));
    }
  }

  async function cancelAi() {
    if (!trackedJobId) return;
    setFailure("");
    try {
      const cancelled = await client.cancelJob(trackedJobId);
      queryClient.setQueryData(["preparation-job", projectId, trackedJobId], cancelled);
      setJobId(null);
      await queryClient.invalidateQueries({ queryKey });
    } catch (error) {
      setFailure(errorMessage(error));
    }
  }

  if (preparation.isLoading) return <section className="preparation-card" aria-busy="true">正在读取创作准备…</section>;
  if (preparation.error || !state) {
    return <p role="alert">创作准备读取失败：{errorMessage(preparation.error)}</p>;
  }
  if (state.status === "not_started") {
    return (
      <section className={`preparation-card preparation-empty${compact ? " preparation-compact preparation-intro" : ""}`}>
        <div>
        <span className="eyebrow">创作准备 · 可跳过</span>
        <h2>先固定会反复用到的规则</h2>
        <p>用最多 5 个本地问题补齐高影响设定。开始整理不会调用模型。</p>
        </div>
        <button className="primary-action" onClick={() => void run(client.initializePreparation)}>
          开始整理
        </button>
      </section>
    );
  }
  if (state.status === "skipped") {
    return (
      <section className={`preparation-card preparation-empty${compact ? " preparation-compact preparation-intro" : ""}`}>
        <div>
        <span className="eyebrow">创作准备 · 已跳过</span>
        <h2>随时回来补齐设定</h2>
        <p>跳过不会阻止写作；生成正文前仍会温和提醒未完成项。</p>
        </div>
        <button className="primary-action" onClick={() => void run(() => client.resumePreparation(state.revision))}>
          继续整理
        </button>
      </section>
    );
  }

  if (compact) {
    return (
      <section className="preparation-card preparation-compact">
        <div>
          <span className="eyebrow">创作准备 · 可选</span>
          <h2>{state.unresolved_high_count
            ? `还有 ${state.unresolved_high_count} 项高影响设定待明确`
            : state.unresolved_count
              ? `还有 ${state.unresolved_count} 项设定待明确`
              : "基础设定已整理"}</h2>
          <p>明确的回答会作为置顶事实进入长篇记忆；问题本身不会污染小说资料。</p>
        </div>
        {onOpenFull && <button onClick={onOpenFull}>打开设定清单</button>}
      </section>
    );
  }

  const canAnalyze = state.round === 1 && state.questions.length < 8 && state.generation_job_ids.length === 0;
  const canFollowUp = state.round === 1 && state.unresolved_count === 0 && state.generation_job_ids.length < 2;
  const terminalJob = job.data && ["failed", "recovery_required", "cancelled"].includes(job.data.status);
  const aiBusy = Boolean(trackedJobId) && !terminalJob && job.data?.status !== "succeeded";
  return (
    <section className={`preparation-card${compact ? " preparation-compact" : ""}`}>
      <header className="preparation-header">
        <div>
          <span className="eyebrow">创作准备 · 第 {state.round} 轮</span>
          <h2>高影响设定清单</h2>
        </div>
        <p role="status">
          已确认 {state.answered_count}/{state.questions.length} · 待明确 {state.unresolved_count}
        </p>
      </header>

      {state.impact_notice && (
        <div className="preparation-impact" role="alert">
          <strong>设定已变更，可能影响已有 {state.impact_notice.chapter_count} 章正文。</strong>
          <span>Agent 不会自动改稿；建议在下一次连续性检查中复核相关章节。</span>
          <button onClick={() => void run(() => client.acknowledgePreparationImpact(state.revision))}>
            我已知晓，关闭提醒
          </button>
        </div>
      )}
      {failure && <p className="form-error" role="alert">{failure}</p>}
      {terminalJob && <p className="form-error" role="alert">智能补充任务未完成：{job.data?.error_message ?? job.data?.status}</p>}
      {trackedJobId && !terminalJob && job.data?.status !== "succeeded" && (
        <p className="preparation-job" role="status">智能补充正在后台分析，不影响继续回答。</p>
      )}
      {job.data?.allowed_actions?.some((action) => action === "resume" || action === "cancel") && (
        <div className="preparation-actions" aria-label="智能补充任务操作">
          {job.data.allowed_actions.includes("resume") && (
            <button onClick={() => void resumeAi()}>恢复智能补充</button>
          )}
          {job.data.allowed_actions.includes("cancel") && (
            <button onClick={() => void cancelAi()}>取消智能补充</button>
          )}
        </div>
      )}

      {currentQuestion && (
        <div className="preparation-question-grid">
          <ol className="preparation-progress" aria-label="问题进度">
            {state.questions.map((item, index) => (
              <li key={item.id} data-status={item.status}>
                <button
                  aria-current={questionIndex === index ? "step" : undefined}
                  aria-label={`打开第 ${index + 1} 个问题，${questionStatusLabels[item.status]}`}
                  onClick={() => setQuestionIndex(index)}
                >
                  {index + 1}
                </button>
              </li>
            ))}
          </ol>
          <article className="preparation-question">
            <p className="preparation-meta">
              问题 {questionIndex + 1} / {state.questions.length} · {currentQuestion.origin === "ai" ? "智能补充" : "本地模板"}
            </p>
            <h3>{currentQuestion.question}</h3>
            <p>{currentQuestion.rationale}</p>
            <p className="preparation-impact-areas">影响：{currentQuestion.impact_areas.join(" · ")}</p>
            <label>
              作者回答
              {currentQuestion.answer_format === "choice" ? (
                <select
                  value={currentDraft}
                  onChange={(event) => setDrafts((items) => ({ ...items, [currentQuestion.id]: event.target.value }))}
                >
                  <option value="">请选择</option>
                  {currentQuestion.options.map((option) => <option key={option}>{option}</option>)}
                </select>
              ) : (
                <textarea
                  maxLength={10_000}
                  rows={currentQuestion.answer_format === "short_text" ? 3 : 6}
                  placeholder={currentQuestion.answer_format === "ordered_list" ? "每行一项，按顺序填写" : "写下你的确定设定"}
                  value={currentDraft}
                  onChange={(event) => setDrafts((items) => ({ ...items, [currentQuestion.id]: event.target.value }))}
                />
              )}
            </label>
            <div className="preparation-actions">
              <button className="primary-action" disabled={!currentDraft.trim()} onClick={() => void submitAnswer()}>
                保存回答
              </button>
              <button onClick={() => void mutateQuestion({ action: "defer" })}>稍后回答</button>
              <button onClick={() => void mutateQuestion({ action: "not_applicable" })}>不适用于本书</button>
            </div>
          </article>
        </div>
      )}

      <footer className="preparation-footer">
        <div>
          {canAnalyze && <button disabled={aiBusy} onClick={() => void startAi("analysis")}>智能补充缺失设定</button>}
          {canFollowUp && <button disabled={aiBusy} onClick={() => void startAi("followup")}>进行一次追问检查</button>}
        </div>
        <div>
          <button className="text-action" onClick={() => void run(() => client.skipPreparation(state.revision))}>
            暂时跳过
          </button>
        </div>
      </footer>
    </section>
  );
}
