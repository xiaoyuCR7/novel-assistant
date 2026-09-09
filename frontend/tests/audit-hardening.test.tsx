import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../src/app/App";
import { emptyLibraryPage, emptyWorkspaceView } from './library-fixtures';
import type { ChapterDocument, ChapterSummary } from "../src/lib/types";
import { ApiError, projectApi, type AIJob, type JobSummary } from "../src/lib/api";
import { pendingSubmissions, prepareSubmission } from "../src/features/ai/pendingSubmission";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

function studio(options: { candidate?: boolean; failedSummary?: boolean; evidence?: boolean; ledgerPending?: boolean; ledgerJob?: boolean; noSummary?: boolean; holdAcceptedRefresh?: boolean; summaryConflict?: boolean; pendingSummary?: boolean; summaryEffect?: unknown; summaryEffects?: unknown; summaryStatus?: string; replacementSource?: boolean; projectionSibling?: boolean } = {}) {
  const project = { id: "hardening-p", title: "Novel", premise: "", genre: "", target_words: 1000, daily_goal: 100, status: "active" };
  const chapter = { id: "hardening-c", kind: "chapter", title: "Chapter", status: "drafting", order_index: 1, parent_id: null };
  let document: ChapterDocument = { content: "original", contract: {}, current_version_id: "v1", revision: 1 };
  let summary: ChapterSummary = {
    id: "summary-1", chapter_id: chapter.id, version_id: "v1", title: "Chapter",
    recap: "recap", details: { character_states: ["at home"], end_state: "night" },
    origin: "ai_generated", provider: "demo", status: "valid", revision: 1, content_hash: "hash",
  };
  if (options.evidence) summary.details = {
    ...summary.details,
    evidence: [{ field: "recap", index: 0, quote: "original", start: 0, end: 8 }],
  };
  let summaryDeleted = !!options.noSummary;
  let chaptersDeleted = false;
  let ledgerPending = !!options.ledgerPending;
  const version = { id: "v1", summary: "first", source: "manual", word_count: 8, created_at: "2026-08-28" };
  const restores: unknown[] = [];
  const saves: Array<{ content: string; revision: number }> = [];
  const summarySaves: Array<{ recap: string; revision: number; summary_id: string; details: ChapterSummary["details"] }> = [];
  let failedRefresh: "projects" | "workspace" | "chapter" | undefined;
  let releaseRestore: ((error?: string) => void) | undefined;
  let releaseSummary: (() => void) | undefined;
  let releaseAccept: (() => void) | undefined;
  let releaseAcceptRefresh: (() => void) | undefined;
  let releaseLedger: ((error?: string, remainsPending?: boolean) => void) | undefined;
  const ledgerRepairs: string[] = [];
  const completions: unknown[] = [];
  const resumes: unknown[] = [];
  const summaryJobFetches: string[] = [];
  const job: AIJob = { id: "job-1", project_id: project.id, chapter_id: chapter.id, task_type: "rewrite", status: "succeeded", context_snapshot: {}, result: { candidate_text: "candidate" }, allowed_actions: [] };
  let summaryJob: AIJob = { ...job, id: "summary-job", task_type: "chapter_summary", status: options.summaryStatus ?? (options.replacementSource ? "cancelled" : "failed"), control_revision: 1, result: {}, allowed_actions: options.replacementSource ? ["replace"] : ["cancel", "resume"] };
  if (options.summaryEffect !== undefined) summaryJob = { ...summaryJob, effects: { summary_id: options.summaryEffect } as AIJob["effects"] };
  const hasRawSummaryEffects = Object.prototype.hasOwnProperty.call(options, "summaryEffects");
  if (hasRawSummaryEffects) summaryJob = { ...summaryJob, effects: options.summaryEffects as AIJob["effects"] };
  if (options.ledgerJob) summaryJob = { ...summaryJob, status: 'succeeded', allowed_actions: [],
    effects: { summary_id: summary.id, ledger_pending: ledgerPending } };
  if (options.failedSummary) document.status = "summary_pending";
  if (options.replacementSource || options.projectionSibling || options.summaryEffect !== undefined || hasRawSummaryEffects) document.status = "summary_pending";
  const projectionJob: AIJob = { ...summaryJob, id: "summary-projection", status: "failed", allowed_actions: [],
    effects: { summary_id: "published-summary" } };
  let conflictVisible = false;
  const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, request?: RequestInit) => {
    const url = String(input);
    const failedPath = failedRefresh === "chapter" ? "/chapters/hardening-c" : failedRefresh === 'workspace' ? '/workspace/navigation' : `/${failedRefresh}`;
    if (failedRefresh && url.endsWith(failedPath)) {
      return response({ detail: { message: `${failedRefresh} refresh failed`, code: "REFRESH_FAILED" } }, 503);
    }
    const otherProject = url.includes("/projects/another-project/");
    if (url.endsWith("/projects")) return response([project, { ...project, id: "another-project", title: "Other novel" }]);
    const page = emptyLibraryPage(url); if (page) return response(page);
    const viewData = emptyWorkspaceView(url, [chapter]); if (viewData) return response(viewData);
    if (url.endsWith('/workspace/navigation') && options.holdAcceptedRefresh && job.accepted_version_id) {
      return new Promise<Response>(resolve => {
        releaseAcceptRefresh = () => resolve(response({ project, nodes: [chapter] }));
      });
    }
    if (url.endsWith('/workspace/navigation')) return response({ project: otherProject ? { ...project, id: "another-project", title: "Other novel" } : project, nodes: chaptersDeleted ? [] : [chapter, { ...chapter, id: "other-chapter", title: "Other chapter", order_index: 2 }] });
    if (url.includes("/ai/jobs/page?")) {
      if (url.includes("kind=summary")) summaryJobFetches.push(url);
      let tasks = url.includes("kind=summary")
        ? (conflictVisible || options.failedSummary || options.ledgerJob || options.replacementSource || options.summaryEffect !== undefined || hasRawSummaryEffects
          ? [summaryJob, ...(options.projectionSibling ? [projectionJob] : [])] : [])
        : (options.candidate ? [job] : []);
      if (url.includes('active_only=true')) tasks = tasks.filter(task => ['queued', 'running', 'cancel_requested', 'recovery_required'].includes(task.status));
      return response({ items: tasks.map(({ result, context_snapshot, ...task }) => ({ ...task, preview: result.candidate_text ?? '' })), next_cursor: null });
    }
    if (url.endsWith('/ai/jobs/job-1')) return response(job);
    if (url.endsWith('/ai/jobs/summary-job')) return response(summaryJob);
    if (url.endsWith("/cancel")) {
      summaryJob = { ...summaryJob, status: "cancelled", allowed_actions: [] };
      return response(summaryJob);
    }
    if (url.endsWith("/resume")) {
      resumes.push(JSON.parse(String(request?.body)));
      return response(summaryJob);
    }
    if (url.includes("/diff/")) return response({ detail: { message: "comparison failed", code: "COMPARE_FAILED" } }, 500);
    if (url.endsWith("/complete")) {
      completions.push(JSON.parse(String(request?.body)));
      if (options.summaryConflict) {
        conflictVisible = true;
        summaryJob = { ...summaryJob, id: "summary-blocker", status: "failed", allowed_actions: ["cancel", "resume"] };
        return response({ detail: { code: "SUMMARY_TASK_ACTIVE", job_id: "summary-blocker" } }, 409);
      }
      summaryJob = { ...summaryJob, id: "summary-job-2", status: "queued", allowed_actions: ["cancel"] };
      return response(summaryJob);
    }
    if (url.endsWith("/accept")) return new Promise<Response>(resolve => {
      releaseAccept = () => {
        document = { ...document, content: "candidate", revision: document.revision! + 1, current_version_id: "v2" };
        job.accepted_version_id = "v2";
        resolve(response({ ...version, id: "v2" }));
      };
    });
    if (url.endsWith("/chapters/hardening-c")) {
      if (otherProject) return response({ ...document, content: "other project text" });
      if (request?.method === "PUT") {
        const data = JSON.parse(String(request.body));
        saves.push(data);
        document = { ...document, ...data, revision: document.revision! + 1 };
      }
      return response(document);
    }
    if (url.endsWith("/restore")) {
      restores.push(request?.body ? JSON.parse(String(request.body)) : null);
      return new Promise<Response>(resolve => {
        releaseRestore = error => {
          if (error) resolve(response({ detail: { message: error, code: "REVISION_CONFLICT" } }, 409));
          else {
            document = { ...document, content: "restored", revision: document.revision! + 1, current_version_id: "v2" };
            resolve(response({ ...version, id: "v2" }));
          }
        };
      });
    }
    if (url.includes("/versions/page?")) return response({
      items: [{
        id: version.id,
        chapter_id: chapter.id,
        created_at: version.created_at,
        source: version.source,
        summary: version.summary,
        word_count: version.word_count,
        parent_version_id: null,
        restored_from_version_id: null,
        generation_job_id: null,
      }],
      next_cursor: null,
    });
    if (url.endsWith("/versions")) return response([version]);
    if (url.endsWith("/summary")) {
      if (request?.method === "PATCH") {
        const data = JSON.parse(String(request.body));
        summarySaves.push(data);
        return new Promise<Response>(resolve => {
          releaseSummary = () => {
            summary = { ...summary, ...data, origin: "author_edited", revision: summary.revision + 1 };
            resolve(response(summary));
          };
        });
      }
      return response(summaryDeleted ? null : { ...summary, ledger_pending: ledgerPending });
    }
    if (url.endsWith("/settings/model")) return response({ mode: "demo", model: "", base_url: "", has_api_key: false, external_consent: false });
    if (url.endsWith("/rag/health")) return response({ vectors: "disabled", documents: 0, ledger_pending: ledgerPending });
    if (url.endsWith("/rag/repair-ledger") || url.endsWith('/summary/repair-ledger')) {
      ledgerRepairs.push(url);
      return new Promise<Response>(resolve => {
        releaseLedger = (error, remainsPending = false) => {
          if (error) resolve(response({ detail: { message: error, code: "LEDGER_REPAIR_FAILED" } }, 503));
          else {
            ledgerPending = remainsPending;
            summaryJob = { ...summaryJob, effects: { ...summaryJob.effects, ledger_pending: remainsPending } };
            resolve(response({ scheduled: true }));
          }
        };
      });
    }
    if (url.endsWith("/progress")) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 2, completed_chapters: 0, daily_goal: 100 });
    return response([]);
  }));
  if (options.pendingSummary) prepareSubmission(project.id, chapter.id, { expected_revision: 1 }, "summary");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const rendered = render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return {
    project, chapter, restores, saves, summarySaves, completions, resumes, ledgerRepairs, client,
    summaryJobFetches,
    deleteSummary: async () => {
      summaryDeleted = true;
      await act(async () => client.invalidateQueries({ queryKey: ["summary", project.id, chapter.id] }));
    },
    deleteChapters: async () => {
      chaptersDeleted = true;
      await act(async () => client.invalidateQueries({ queryKey: ["workspace", project.id] }));
    },
    repairLedger: async (error?: string, remainsPending?: boolean) => {
      await waitFor(() => expect(releaseLedger).toBeTypeOf("function"));
      await act(async () => releaseLedger!(error, remainsPending));
    },
    remountOtherProject: () => {
      rendered.unmount();
      localStorage.setItem("studio:active-project", "another-project");
      render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
    },
    accept: async () => {
      await waitFor(() => expect(releaseAccept).toBeTypeOf("function"));
      await act(async () => releaseAccept!());
    },
    finishAcceptRefresh: async () => {
      await waitFor(() => expect(releaseAcceptRefresh).toBeTypeOf("function"));
      await act(async () => releaseAcceptRefresh!());
    },
    restore: async (error?: string) => {
      await waitFor(() => expect(releaseRestore).toBeTypeOf("function"));
      await act(async () => releaseRestore!(error));
    },
    saveSummary: async () => {
      await waitFor(() => expect(releaseSummary).toBeTypeOf("function"));
      await act(async () => releaseSummary!());
    },
    refreshSummary: async () => {
      summary = { ...summary, recap: "server recap", revision: summary.revision + 1 };
      await act(async () => client.invalidateQueries({ queryKey: ["summary", project.id, chapter.id] }));
    },
    failRefresh: async (key: "projects" | "workspace" | "chapter") => {
      failedRefresh = key;
      await act(async () => client.invalidateQueries({ queryKey: key === "projects" ? [key] : [key, project.id] }));
    },
    replaceSummary: async () => {
      summary = { ...summary, id: "summary-2", recap: "new summary", revision: 1 };
      await act(async () => client.invalidateQueries({ queryKey: ["summary", project.id, chapter.id] }));
    },
  };
}

async function openEditor() {
  await screen.findByLabelText("给 AI 的消息");
  await userEvent.click(screen.getByRole("button", { name: "查看正文" }));
  return screen.findByLabelText("章节正文");
}

async function startRestore() {
  await userEvent.click(screen.getByText("历史版本"));
  await userEvent.click(screen.getByRole("button", { name: "从first创建恢复版本" }));
}

async function editSummary() {
  await userEvent.click(screen.getByText("章节总结与连续性"));
  await userEvent.click(screen.getByRole("button", { name: "编辑总结" }));
  return screen.getByLabelText("章节总结");
}

it("preserves the blocking summary job identity on API conflicts", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
    detail: { code: "SUMMARY_TASK_ACTIVE", job_id: "summary-blocker" },
  }), { status: 409, headers: { "Content-Type": "application/json" } })));

  const error = await projectApi("hardening-p")
    .completeChapter("hardening-c", { expected_revision: 1 }, "conflict-key")
    .catch(value => value);

  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 409, code: "SUMMARY_TASK_ACTIVE", jobId: "summary-blocker" });
});

it("acknowledges a summary conflict, refreshes its blocker, and directs the author", async () => {
  const app = studio({ summaryConflict: true });
  await openEditor();
  const initialFetches = app.summaryJobFetches.length;

  await userEvent.click(screen.getByRole("button", { name: "完成本章" }));

  expect(await screen.findByText(/summary-blocker/)).toBeInTheDocument();
  await waitFor(() => expect(app.summaryJobFetches.length).toBeGreaterThan(initialFetches));
  expect(await screen.findByText("执行失败")).toBeInTheDocument();
  expect(screen.queryByText(/总结任务已提交/)).not.toBeInTheDocument();
  expect(pendingSubmissions(app.project.id)).toEqual([]);
});

it("keeps the blocker guidance when a persisted summary submission is replayed", async () => {
  const app = studio({ pendingSummary: true, summaryConflict: true });
  await screen.findByLabelText("给 AI 的消息");

  await userEvent.click(screen.getByRole("button", { name: "用原请求确认提交" }));

  expect(await screen.findByText(/summary-blocker/)).toHaveTextContent("请在任务卡片中恢复、取消或替换");
  expect(pendingSubmissions(app.project.id)).toEqual([]);
});

it.each([
  ["inherited", Object.assign(Object.create({ summary_id: "inherited-summary" }), { ledger_pending: false })],
  ["null", null],
  ["array", Object.assign([], { summary_id: "array-summary" })],
])("requires an own summary_id property for %s effects", async (_label, effects) => {
  studio({ summaryStatus: "failed", summaryEffects: effects });
  await openEditor();
  expect(screen.queryByRole("button", { name: "重试章节总结" })).not.toBeInTheDocument();
  expect(screen.queryByText(/AI 总结已保存/)).not.toBeInTheDocument();
});

it.each(["failed", "recovery_required", "succeeded"])(
  "does not let a published %s summary projection block a fresh completion",
  async status => {
    studio({ summaryStatus: status, summaryEffect: "  published-summary  " });
    await openEditor();
    expect(screen.getByRole("button", { name: "重试章节总结" })).toBeEnabled();
  },
);

it("does not let a published projection sibling disable explicit replacement", async () => {
  studio({ replacementSource: true, projectionSibling: true });
  await openEditor();
  expect(screen.getByRole("button", { name: "按当前设置新建" })).toBeEnabled();
});

it.each([" ", 1, true, [], {}])(
  "keeps invalid published summary identity %j blocking without projection refresh",
  async summaryId => {
    studio({ summaryStatus: "failed", summaryEffect: summaryId });
    await openEditor();
    expect(screen.queryByRole("button", { name: "重试章节总结" })).not.toBeInTheDocument();
    expect(screen.queryByText(/AI 总结已保存/)).not.toBeInTheDocument();
  },
);

it("preserves typing made during restore and advances the draft save revision", async () => {
  const app = studio();
  const editor = await openEditor();
  await startRestore();
  await userEvent.type(editor, " UNSAVED");
  await app.restore();
  await waitFor(() => expect(screen.getByRole("button", { name: "保存工作副本" })).toBeEnabled());
  expect(screen.getByLabelText("章节正文")).toBe(editor);
  expect(editor).toHaveValue("original UNSAVED");
  expect(screen.getByText(/恢复期间.*保留/)).toBeInTheDocument();
  expect(app.restores).toEqual([{ expected_revision: 1 }]);
  await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
  expect(app.saves[0]).toMatchObject({ content: "original UNSAVED", revision: 2 });
});

it("applies an explicitly confirmed restore when no later edits were made", async () => {
  const app = studio();
  const editor = await openEditor();
  await userEvent.type(editor, " discard");
  vi.spyOn(window, "confirm").mockReturnValue(true);
  await startRestore();
  await app.restore();
  await waitFor(() => expect(editor).toHaveValue("restored"));
  expect(screen.getByLabelText("章节正文")).toBe(editor);
  expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
});

it("locks duplicate restore, conflicting mutations and navigation until the request settles", async () => {
  const app = studio();
  const editor = await openEditor();
  const recap = await editSummary();
  await userEvent.type(recap, " unsaved summary");
  vi.spyOn(window, "confirm").mockReturnValue(true);
  await startRestore();
  const restore = screen.getByRole("button", { name: "从first创建恢复版本" });
  await userEvent.click(restore);
  expect(restore).toBeDisabled();
  expect(app.restores).toHaveLength(1);
  expect(screen.getByRole("button", { name: /保存中|保存工作副本/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: "完成本章" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "保存总结" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  await userEvent.click(screen.getByRole("button", { name: /Other chapter/ }));
  await userEvent.selectOptions(screen.getByLabelText("当前小说项目"), "another-project");
  await userEvent.click(screen.getByRole("button", { name: "收起正文" }));
  expect(screen.getByLabelText("章节正文")).toBe(editor);
  expect(screen.getByLabelText("当前小说项目")).toHaveValue(app.project.id);
  await app.restore();
  expect(recap).toHaveValue("recap unsaved summary");
});

it("shows restore errors and keeps the draft and actions available for retry", async () => {
  const app = studio();
  const editor = await openEditor();
  await startRestore();
  await userEvent.type(editor, " UNSAVED");
  await app.restore("revision conflict");
  expect(await screen.findByRole("alert")).toHaveTextContent("revision conflict");
  expect(editor).toHaveValue("original UNSAVED");
  expect(screen.getByRole("button", { name: "从first创建恢复版本" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "保存工作副本" })).toBeEnabled();
});

it("keeps summary dirty after the manuscript saves and guards project and view changes", async () => {
  studio();
  const editor = await openEditor();
  const recap = await editSummary();
  await userEvent.type(recap, " UNSAVED");
  await userEvent.type(editor, " saved");
  await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "保存工作副本" })).toBeEnabled());
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  await userEvent.selectOptions(screen.getByLabelText("当前小说项目"), "another-project");
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  expect(recap).toHaveValue("recap UNSAVED");
});

it("keeps active summary edits and their original revision across a server refresh", async () => {
  const app = studio();
  await openEditor();
  const recap = await editSummary();
  await userEvent.type(recap, " UNSAVED");
  await app.refreshSummary();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/总结.*更新.*保留/));
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  expect(recap).toHaveValue("recap UNSAVED");
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  expect(app.summarySaves[0].revision).toBe(1);
  expect(app.summarySaves[0].summary_id).toBe("summary-1");
  await app.saveSummary();
});

it("preserves new summary typing when an earlier save resolves", async () => {
  const app = studio();
  await openEditor();
  const recap = await editSummary();
  await userEvent.type(recap, " saved");
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  await userEvent.type(recap, " UNSAVED");
  await app.saveSummary();
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  expect(recap).toHaveValue("recap saved UNSAVED");
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  expect(app.summarySaves[1].revision).toBe(2);
  await app.saveSummary();
});

it("keeps pending restore and dirty summary protected from the trash shortcut", async () => {
  const app = studio();
  const editor = await openEditor();
  await startRestore();
  await userEvent.click(screen.getByRole("button", { name: "参考资料" }));
  await userEvent.click(screen.getByRole("button", { name: /回收站/ }));
  expect(screen.getByLabelText("章节正文")).toBe(editor);
  await userEvent.click(screen.getByRole("button", { name: "关闭对话框" }));
  await app.restore();
  const recap = await editSummary();
  await userEvent.type(recap, " UNSAVED");
  vi.spyOn(window, "confirm").mockReturnValue(false);
  await userEvent.click(screen.getByRole("button", { name: "参考资料" }));
  await userEvent.click(screen.getByRole("button", { name: /回收站/ }));
  expect(screen.getByLabelText("章节总结")).toBe(recap);
});

it("preserves new manuscript typing during candidate acceptance", async () => {
  const app = studio({ candidate: true });
  const editor = await openEditor();
  await userEvent.click(screen.getByRole("button", { name: "写入正文" }));
  await userEvent.type(editor, " UNSAVED");
  await app.accept();
  expect(screen.getByLabelText("章节正文")).toBe(editor);
  expect(editor).toHaveValue("original UNSAVED");
  expect(screen.getByText(/采纳期间.*保留/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
  expect(app.saves[0].revision).toBe(2);
});

it.each([false, true])("reports successful acceptance after saving manuscript and purpose (collapsed=%s)", async (collapsed) => {
  const app = studio({ candidate: true, holdAcceptedRefresh: true });
  const editor = await openEditor();
  await userEvent.clear(editor);
  await userEvent.type(editor, "saved draft");
  await userEvent.type(screen.getByLabelText("本章目的"), "keep the parcel");
  await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
  await waitFor(() => expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument());
  if (collapsed) await userEvent.click(screen.getByRole("button", { name: "收起正文" }));
  await userEvent.click(screen.getByRole("button", { name: "写入正文" }));
  await app.accept();
  if (!collapsed) await waitFor(() => expect(editor).toHaveValue("candidate"));
  await app.finishAcceptRefresh();
  expect(await screen.findByText("已写入正文，并保存为新版本。")).toBeVisible();
  expect(screen.getByLabelText("章节正文")).toHaveValue("candidate");
  expect(screen.getByLabelText("本章目的")).toHaveValue("keep the parcel");
  expect(screen.queryByText(/采纳期间.*保留/)).not.toBeInTheDocument();
  expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
});

it("preserves author input made after the accepted document arrives but before refresh finishes", async () => {
  const app = studio({ candidate: true, holdAcceptedRefresh: true });
  const editor = await openEditor();
  await userEvent.click(screen.getByRole("button", { name: "写入正文" }));
  await app.accept();
  await waitFor(() => expect(editor).toHaveValue("candidate"));
  await userEvent.type(editor, " UNSAVED");
  await app.finishAcceptRefresh();
  expect(editor).toHaveValue("candidate UNSAVED");
  expect(screen.getByText(/采纳期间.*保留/)).toBeInTheDocument();
  expect(screen.getByText("有未保存修改")).toBeInTheDocument();
});

it("keeps a late restore response scoped to the unmounted original project", async () => {
  const app = studio();
  await openEditor();
  await startRestore();
  app.remountOtherProject();
  const otherEditor = await openEditor();
  expect(otherEditor).toHaveValue("other project text");
  await app.restore();
  expect(screen.getByLabelText("章节正文")).toBe(otherEditor);
  expect(otherEditor).toHaveValue("other project text");
  expect(screen.queryByText(/已创建恢复版本|恢复期间/)).not.toBeInTheDocument();
  expect(app.client.getQueryData(["chapter", app.project.id, app.chapter.id])).toMatchObject({ content: "restored", revision: 2 });
});

it("allows an explicit fresh completion only after the failed summary is cancelled", async () => {
  const app = studio({ failedSummary: true });
  await openEditor();
  expect(screen.queryByRole("button", { name: "重试章节总结" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "取消任务" }));
  await userEvent.click(await screen.findByRole("button", { name: "重试章节总结" }));
  await waitFor(() => expect(app.completions).toEqual([{ expected_revision: 2 }]));
});

it("does not resume a conflicting summary task while restore is pending", async () => {
  const app = studio({ failedSummary: true });
  await openEditor();
  await startRestore();
  await userEvent.click(screen.getByRole("button", { name: "恢复任务" }));
  expect(app.resumes).toHaveLength(0);
  expect(screen.getByRole("alert")).toHaveTextContent("请等待当前请求结束后再操作");
  await app.restore();
});

it("shows a comparison failure without hiding version controls", async () => {
  studio();
  await openEditor();
  await userEvent.click(screen.getByText("历史版本"));
  await userEvent.click(screen.getByRole("button", { name: "比较版本" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("comparison failed");
  expect(screen.getByRole("button", { name: "从first创建恢复版本" })).toBeEnabled();
});

it.each(["chapter", "workspace", "projects"] as const)("retains dirty manuscript and summary after a background %s refresh fails", async key => {
  const app = studio();
  const editor = await openEditor();
  const recap = await editSummary();
  await userEvent.type(editor, " BODY UNSAVED");
  await userEvent.type(recap, " SUMMARY UNSAVED");
  await app.failRefresh(key);
  await screen.findByText(new RegExp(`${key} refresh failed`));
  expect(screen.getByLabelText("章节正文")).toBe(editor);
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  expect(editor).toHaveValue("original BODY UNSAVED");
  expect(recap).toHaveValue("recap SUMMARY UNSAVED");
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await userEvent.selectOptions(screen.getByLabelText("当前小说项目"), "another-project");
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("当前小说项目")).toHaveValue(app.project.id);
});

it("requires an explicit new baseline when summary identity changes at the same revision", async () => {
  const app = studio();
  await openEditor();
  const recap = await editSummary();
  await userEvent.type(recap, " UNSAVED");
  await app.replaceSummary();
  await waitFor(() => expect(screen.getByRole("button", { name: "保存总结" })).toBeDisabled());
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  expect(recap).toHaveValue("recap UNSAVED");
  expect(screen.getByRole("alert")).toHaveTextContent(/新总结.*保留/);
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  expect(app.summarySaves).toHaveLength(0);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await userEvent.click(screen.getByRole("button", { name: "以当前总结为基线继续编辑" }));
  expect(screen.getByRole("button", { name: "保存总结" })).toBeDisabled();
  confirm.mockReturnValue(true);
  await userEvent.click(screen.getByRole("button", { name: "以当前总结为基线继续编辑" }));
  expect(recap).toHaveValue("recap UNSAVED");
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  expect(app.summarySaves[0]).toMatchObject({ recap: "recap UNSAVED", revision: 1, summary_id: "summary-2" });
  await app.saveSummary();
});

it("keeps a deleted summary draft copyable but never saves it without an explicit discard", async () => {
  const app = studio();
  await openEditor();
  const recap = await editSummary();
  await userEvent.type(recap, " UNSAVED");
  await app.deleteSummary();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/总结已删除.*保留/));
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  expect(recap).toHaveValue("recap UNSAVED");
  expect(recap).toBeEnabled();
  expect(screen.getByRole("button", { name: "保存总结" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  expect(app.summarySaves).toHaveLength(0);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await userEvent.selectOptions(screen.getByLabelText("当前小说项目"), "another-project");
  expect(screen.getByLabelText("当前小说项目")).toHaveValue(app.project.id);
  await userEvent.click(screen.getByRole("button", { name: "放弃已删除总结的本地草稿" }));
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  confirm.mockReturnValue(true);
  await userEvent.click(screen.getByRole("button", { name: "放弃已删除总结的本地草稿" }));
  expect(screen.queryByLabelText("章节总结")).not.toBeInTheDocument();
  confirm.mockClear();
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  expect(confirm).not.toHaveBeenCalled();
  expect(app.summarySaves).toHaveLength(0);
});

it("releases the project busy state when the original chapter disappears during restore", async () => {
  const app = studio();
  await openEditor();
  await startRestore();
  await app.deleteChapters();
  await screen.findByText("先在故事地图中建立章节");
  await app.restore();
  await waitFor(() => expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled());
  expect(screen.queryByText("已创建恢复版本。")).not.toBeInTheDocument();
});

it("retains a deleted summary draft after reopening the same chapter workspace", async () => {
  const app = studio();
  await openEditor();
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  await userEvent.click(screen.getByRole("button", { name: "创作" }));
  const recap = await editSummary();
  await userEvent.type(recap, " UNSAVED after return");
  await app.deleteSummary();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/总结已删除.*保留/));
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  expect(recap).toHaveValue("recap UNSAVED after return");
});

it("retains the local summary after deletion even when its earlier save settles", async () => {
  const app = studio();
  await openEditor();
  const recap = await editSummary();
  await userEvent.type(recap, " local copy");
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  await app.deleteSummary();
  await app.saveSummary();
  expect(screen.getByLabelText("章节总结")).toBe(recap);
  expect(recap).toHaveValue("recap local copy");
  expect(screen.getByRole("button", { name: "保存总结" })).toBeDisabled();
  expect(screen.getByText("总结有未保存修改")).toBeInTheDocument();
});

it("shows source evidence read-only and does not submit it as evidence for author edits", async () => {
  const app = studio({ evidence: true });
  await openEditor();
  const recap = await editSummary();
  await userEvent.click(screen.getByText("修订结构化记录"));
  expect(screen.queryByLabelText("evidence")).not.toBeInTheDocument();
  await userEvent.click(screen.getByText("原文证据（只读）"));
  expect(screen.getByText("original", { selector: "blockquote" })).toBeInTheDocument();
  expect(screen.getByText(/位置 0–8/)).toBeInTheDocument();
  await userEvent.type(recap, " author correction");
  expect(screen.getByText(/作者修改未重新校验证据/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  expect(app.summarySaves[0].details).not.toHaveProperty("evidence");
  await app.saveSummary();
  expect(screen.getByText(/作者修改未附带新的 AI 原文校验/)).toBeInTheDocument();
});

it("shows pending ledger state on the summary and repairs only the local project ledger", async () => {
  const app = studio({ ledgerPending: true });
  await openEditor();
  await userEvent.click(screen.getByText("章节总结与连续性"));
  expect(screen.getByText(/总结已保存.*账本待修复/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "参考资料" }));
  const repair = await screen.findByRole("button", { name: "仅修复本地账本" });
  await userEvent.click(repair);
  expect(repair).toBeDisabled();
  await userEvent.click(repair);
  expect(app.ledgerRepairs).toHaveLength(1);
  await app.repairLedger();
  await waitFor(() => expect(screen.queryByRole("button", { name: "仅修复本地账本" })).not.toBeInTheDocument());
  expect(app.ledgerRepairs[0]).toBe(`/api/v1/projects/${app.project.id}/rag/repair-ledger`);
  expect(app.completions).toHaveLength(0);
  expect(app.resumes).toHaveLength(0);
});

it("keeps local ledger repair available without a summary and reports errors and pending work", async () => {
  const app = studio({ ledgerPending: true, noSummary: true });
  await openEditor();
  await userEvent.click(screen.getByRole("button", { name: "参考资料" }));
  const repair = await screen.findByRole("button", { name: "仅修复本地账本" });
  await userEvent.click(repair);
  await app.repairLedger("disk unavailable");
  expect(await screen.findByRole("alert")).toHaveTextContent("disk unavailable");
  expect(repair).toBeEnabled();
  await userEvent.click(repair);
  await app.repairLedger(undefined, true);
  await waitFor(() => expect(repair).toBeEnabled());
  expect(screen.getByText(/账本仍待修复/)).toBeInTheDocument();
  expect(app.completions).toHaveLength(0);
  expect(app.summarySaves).toHaveLength(0);
});

it.each(['chapter', 'project'])('refreshes all project ledger views after %s repair without invalidating another project', async entry => {
  const app = studio({ ledgerPending: true, ledgerJob: true });
  await openEditor();
  const scopedKeys = [
    ['summary', app.project.id, 'other-chapter'],
    ['jobs', app.project.id, 'other-chapter', 'summary'],
  ];
  const otherKeys = [['rag', 'unrelated'], ['summary', 'unrelated', 'chapter'], ['jobs', 'unrelated', 'chapter', 'summary']];
  for (const key of [...scopedKeys, ...otherKeys]) app.client.setQueryData(key, { marker: 'untouched' });
  let repair = screen.getByRole('button', { name: '仅修复本地账本' });
  await userEvent.click(screen.getByRole('button', { name: '参考资料' }));
  const status = await screen.findByText('本地连续性账本待修复。仅更新本地文件，不调用模型。');
  if (entry === 'project') {
    repair = within(status.parentElement!).getByRole('button', { name: '仅修复本地账本' });
  }
  await userEvent.click(repair);
  await app.repairLedger();
  await waitFor(() => {
    expect(app.client.getQueryData<{ ledger_pending: boolean }>(['rag', app.project.id])?.ledger_pending).toBe(false);
    expect(app.client.getQueryData<{ ledger_pending: boolean }>(['summary', app.project.id, app.chapter.id])?.ledger_pending).toBe(false);
    const jobs = app.client.getQueryData<{ pages: Array<{ items: JobSummary[] }> }>(['jobs', app.project.id, app.chapter.id, 'summary']);
    expect(jobs?.pages[0].items[0].effects?.ledger_pending).toBe(false);
  });
  for (const key of scopedKeys) expect(app.client.getQueryState(key)?.isInvalidated).toBe(true);
  for (const key of otherKeys) expect(app.client.getQueryState(key)?.isInvalidated).toBe(false);
  expect(app.completions).toHaveLength(0);
  expect(app.resumes).toHaveLength(0);
});
