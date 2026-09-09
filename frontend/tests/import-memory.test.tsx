import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "../src/app/App";
import { MemoryCandidateReview } from "../src/features/memory/MemoryCandidateReview";
import { ImportAnalysisPanel } from "../src/features/projects/ImportAnalysisPanel";
import { emptyLibraryPage } from "./library-fixtures";
import type { ImportAnalysis, ImportMemoryCandidate } from "../src/lib/types";

const analysis: ImportAnalysis = {
  id: "batch-1", project_id: "project-1", status: "paused", revision: 3,
  progress: { total: 10, completed: 6, failed: 1, queued: 3, running: 0 },
  current_unit: null, last_error: "模型暂不可用", created_at: "2026-09-01", updated_at: "2026-09-01",
};

const candidate: ImportMemoryCandidate = {
  origin: "import", id: "candidate-1", project_id: "project-1", import_batch_id: "batch-1",
  source_document_id: "source-1", chapter_id: null, source_version_id: null, kind: "entity",
  payload: { name: "林渡", kind: "character" },
  evidence: [{ quote: "林渡走进旧城", start: 0, end: 7, relative_path: "人物.md" }],
  source_hash: "a".repeat(64), dedupe_key: "b".repeat(64), analysis_identity: null,
  status: "conflict", revision: 2,
  conflict: { code: "CANDIDATE_REFERENCE_AMBIGUOUS", record_ids: ["entity-1", "entity-2"] },
  promoted_type: null, promoted_record_id: null,
  promotion_fingerprint: null, created_at: "2026-09-01", updated_at: "2026-09-01",
};

const pendingCandidate: ImportMemoryCandidate = {
  ...candidate,
  id: "candidate-2",
  kind: "canon",
  payload: { predicate: "旧城钟声", value: "只在雨夜响起" },
  evidence: [{ quote: "<img src=x onerror=alert(1)>", start: 8, end: 36, relative_path: "资料/<危险>.md" }],
  status: "pending",
  conflict: {},
  revision: 5,
};

const project = { id: "project-1", title: "旧城", premise: "", genre: "", target_words: 1000, daily_goal: 100, status: "active" };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

function appFetch(options: {
  analyses?: ImportAnalysis[];
  candidates?: ImportMemoryCandidate[];
  onRequest?: (url: string, init?: RequestInit) => Promise<Response> | Response | undefined;
} = {}) {
  const analyses = [...(options.analyses ?? [analysis])];
  const candidates = options.candidates ?? [candidate, pendingCandidate];
  let analysisCalls = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const override = options.onRequest?.(url, init);
    if (override) return override;
    const page = emptyLibraryPage(url); if (page) return response(page);
    if (url.endsWith("/projects")) return response([project]);
    if (url.endsWith("/workspace/navigation")) return response({ project, nodes: [] });
    if (url.includes("/ai/jobs/page?")) return response({ items: [], next_cursor: null });
    if (url.endsWith("/imports/latest/analysis")) {
      const value = analyses[Math.min(analysisCalls, analyses.length - 1)];
      analysisCalls += 1;
      return response(value);
    }
    if (url.includes("/memory-candidates?")) {
      const requestedStatus = new URL(url, "http://local").searchParams.get("status");
      const items = candidates.filter((item) => item.status === requestedStatus);
      return response({
        items,
        total: items.length,
        counts: {
          pending: candidates.filter((item) => item.status === "pending").length,
          conflict: candidates.filter((item) => item.status === "conflict").length,
        },
        next_cursor: null,
      });
    }
    if (url.endsWith("/settings/model")) return response({ mode: "demo", model: "", base_url: "", has_api_key: false, external_consent: false });
    if (url.endsWith("/rag/health")) return response({ vectors: "disabled", documents: 0 });
    if (url.endsWith("/progress")) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 0, completed_chapters: 0, daily_goal: 100 });
    return response([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, get analysisCalls() { return analysisCalls; } };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function renderApp(client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })) {
  const view = render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return { ...view, client };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("shows safe import progress errors and exposes only actions allowed by the current status", async () => {
  const onContinue = vi.fn();
  const { rerender } = render(<ImportAnalysisPanel analysis={analysis} busy={false} onPause={vi.fn()} onContinue={onContinue} onRetry={vi.fn()} />);
  expect(screen.getByText("已分析 6/10")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("模型暂不可用");
  expect(screen.queryByRole("button", { name: "暂停分析" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "继续分析" }));
  expect(onContinue).toHaveBeenCalledWith(3);

  rerender(<ImportAnalysisPanel analysis={{ ...analysis, status: "analyzing" }} busy={false} onPause={vi.fn()} onContinue={vi.fn()} onRetry={vi.fn()} />);
  expect(screen.getByRole("button", { name: "暂停分析" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "继续分析" })).not.toBeInTheDocument();

  rerender(<ImportAnalysisPanel analysis={{ ...analysis, status: "analysis_failed" }} busy={false} onPause={vi.fn()} onContinue={vi.fn()} onRetry={vi.fn()} />);
  expect(screen.getByRole("button", { name: "重试失败项" })).toBeEnabled();

  rerender(<ImportAnalysisPanel analysis={{ ...analysis, status: "analyzed" }} busy={false} onPause={vi.fn()} onContinue={vi.fn()} onRetry={vi.fn()} />);
  expect(screen.queryByRole("button", { name: /暂停|继续|重试/ })).not.toBeInTheDocument();

  const onStart = vi.fn();
  rerender(<ImportAnalysisPanel analysis={{ ...analysis, status: "imported", revision: 1 }} busy={false} onPause={vi.fn()} onContinue={onStart} onRetry={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "开始分析" }));
  expect(onStart).toHaveBeenCalledWith(1);
  rerender(<ImportAnalysisPanel analysis={{ ...analysis, status: "imported", revision: 1 }} busy onPause={vi.fn()} onContinue={onStart} onRetry={vi.fn()} />);
  expect(screen.getByRole("button", { name: "开始分析" })).toBeDisabled();
});

it("contains a rejected analysis action so the project-level error UI can handle it", async () => {
  render(<ImportAnalysisPanel
    analysis={analysis}
    busy={false}
    onPause={vi.fn()}
    onContinue={() => Promise.reject(new Error("request failed"))}
    onRetry={vi.fn()}
  />);
  await userEvent.click(screen.getByRole("button", { name: "继续分析" }));
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByRole("button", { name: "继续分析" })).toBeEnabled();
});

it("renders labeled escaped evidence, side-by-side conflicts, and an ambiguity edit form", async () => {
  const onEdit = vi.fn();
  render(<MemoryCandidateReview candidates={[candidate, pendingCandidate]} busyId={null} onEdit={onEdit} onConfirm={vi.fn()} onReject={vi.fn()} />);

  expect(screen.getByText("名称")).toBeVisible();
  expect(screen.getByText("候选内容")).toBeVisible();
  expect(screen.getByText("现有冲突")).toBeVisible();
  expect(screen.getByText(/请编辑候选内容中的歧义引用/)).toBeVisible();
  expect(screen.getByText("林渡走进旧城")).toBeVisible();
  expect(screen.getByText("人物.md · 0–7")).toBeVisible();
  expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeVisible();
  expect(document.querySelector("img")).toBeNull();
  expect(screen.getByText("资料/<危险>.md · 8–36")).toBeVisible();

  await userEvent.click(screen.getByRole("button", { name: "编辑人物候选" }));
  fireEvent.change(screen.getByLabelText("人物候选 payload JSON"), {
    target: { value: JSON.stringify({ name: "林渡（导入）", kind: "character" }) },
  });
  await userEvent.click(screen.getByRole("button", { name: "保存人物候选" }));
  expect(onEdit).toHaveBeenCalledWith("candidate-1", 2, { name: "林渡（导入）", kind: "character" }, candidate.evidence);
});

it("supports selection and bulk confirm while preventing conflicts from entering the bulk request", async () => {
  const onConfirm = vi.fn();
  const onReject = vi.fn();
  const onBulkConfirm = vi.fn();
  const onLoadMore = vi.fn();
  render(<MemoryCandidateReview candidates={[candidate, pendingCandidate]} busyId={null} onEdit={vi.fn()} onConfirm={onConfirm} onReject={onReject} onBulkConfirm={onBulkConfirm} hasMore onLoadMore={onLoadMore} />);

  expect(screen.getByLabelText("选择人物候选")).toBeDisabled();
  expect(screen.getByRole("button", { name: "批量确认已选（0）" })).toBeDisabled();
  await userEvent.click(screen.getByLabelText("选择世界事实候选"));
  expect(screen.getByRole("button", { name: "批量确认已选（1）" })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: "批量确认已选（1）" }));
  expect(onBulkConfirm).toHaveBeenCalledWith([{ candidate_id: "candidate-2", revision: 5 }]);
  await userEvent.click(screen.getByRole("button", { name: "确认世界事实候选" }));
  expect(onConfirm).toHaveBeenCalledWith("candidate-2", 5, undefined);
  await userEvent.click(screen.getByRole("button", { name: "拒绝人物候选" }));
  expect(onReject).toHaveBeenCalledWith("candidate-1", 2);
  await userEvent.click(screen.getByRole("button", { name: "加载更多待确认记忆" }));
  expect(onLoadMore).toHaveBeenCalledOnce();
});

it("keeps the project usable when the model is unavailable and opens progress/review from a compact card with a pending count", async () => {
  appFetch();
  renderApp();

  expect(await screen.findByText("已分析 6/10")).toBeVisible();
  expect(await screen.findByRole("button", { name: "审核待确认记忆" })).toHaveTextContent("待确认记忆 2");
  expect(screen.getByRole("textbox", { name: "给 AI 的消息" })).toBeEnabled();
  expect(screen.queryByRole("heading", { name: "待确认记忆" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "审核待确认记忆" }));
  expect(await screen.findByRole("heading", { name: "待确认记忆" })).toBeVisible();
  expect(screen.getByText("林渡走进旧城")).toBeVisible();
  expect(screen.getByRole("button", { name: "编辑人物候选" })).toBeEnabled();
});

it("does not add import analysis UI to a project without an import batch", async () => {
  appFetch({ onRequest: (url) => url.endsWith("/imports/latest/analysis")
    ? response({ detail: { code: "IMPORT_BATCH_NOT_FOUND" } }, 404)
    : undefined });
  renderApp();

  expect(await screen.findByRole("textbox", { name: "给 AI 的消息" })).toBeEnabled();
  expect(screen.queryByRole("region", { name: "导入分析摘要" })).not.toBeInTheDocument();
});

it("sends selected candidates to bulk confirm and invalidates all affected project views", async () => {
  let bulkBody: unknown;
  appFetch({
    candidates: [pendingCandidate],
    onRequest: (url, init) => {
      if (url.endsWith("/memory-candidates/bulk-confirm")) {
        bulkBody = JSON.parse(String(init?.body));
        return response({ items: [{ ...pendingCandidate, status: "confirmed", revision: 6 }] });
      }
      return undefined;
    },
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  renderApp(client);

  await userEvent.click(await screen.findByRole("button", { name: "审核待确认记忆" }));
  await userEvent.click(await screen.findByLabelText("选择世界事实候选"));
  await userEvent.click(screen.getByRole("button", { name: "批量确认已选（1）" }));
  await waitFor(() => expect(bulkBody).toEqual({ entries: [{ candidate_id: "candidate-2", revision: 5 }] }));
  for (const key of [
    ["import-memory-candidates", "project-1"], ["import-memory-candidate-count", "project-1"],
    ["workspace", "project-1"], ["library", "project-1"], ["context", "project-1"],
    ["summary", "project-1"], ["progress", "project-1"], ["import-analysis", "project-1"],
    ["style-rules", "project-1"], ["preferences", "project-1"],
  ]) expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: key }));
  expect(invalidate).not.toHaveBeenCalledWith(expect.objectContaining({
    queryKey: ["memory-candidates", "project-1"],
  }));
  expect(invalidate.mock.calls.some(([filters]) => filters?.refetchType === "all")).toBe(false);
});

it("refreshes candidate pages after a revision conflict", async () => {
  let candidateGets = 0;
  let patchCalls = 0;
  appFetch({
    candidates: [candidate],
    onRequest: (url, init) => {
      if (url.includes("/memory-candidates?") && init?.method !== "PATCH") {
        candidateGets += 1;
        const current = { ...candidate, revision: patchCalls > 0 ? 3 : 2 };
        const requestedStatus = new URL(url, "http://local").searchParams.get("status");
        return response({ items: current.status === requestedStatus ? [current] : [], total: 1, counts: { conflict: 1 }, next_cursor: null });
      }
      if (url.endsWith("/memory-candidates/candidate-1") && init?.method === "PATCH") {
        patchCalls += 1;
        return response({ detail: { code: "revision_conflict", current: { ...candidate, revision: 3 } } }, 409);
      }
      return undefined;
    },
  });
  renderApp();

  await userEvent.click(await screen.findByRole("button", { name: "审核待确认记忆" }));
  await userEvent.click(await screen.findByRole("button", { name: "编辑人物候选" }));
  fireEvent.change(screen.getByLabelText("人物候选 payload JSON"), { target: { value: JSON.stringify({ name: "新名字", kind: "character" }) } });
  await userEvent.click(screen.getByRole("button", { name: "保存人物候选" }));
  expect(await screen.findByText(/候选记忆操作失败：revision_conflict/)).toBeVisible();
  await waitFor(() => expect(candidateGets).toBeGreaterThanOrEqual(4));
});

it.each([
  { status: "imported" as const, actionName: "开始分析" },
  { status: "paused" as const, actionName: "继续分析" },
])("refreshes non-polling $status analysis state after the real revision conflict code", async ({ status, actionName }) => {
  let analysisGets = 0;
  const actionRevisions: number[] = [];
  const initial = { ...analysis, status, revision: 1, last_error: "" };
  appFetch({
    candidates: [],
    onRequest: (url, init) => {
      if (url.endsWith("/imports/latest/analysis")) {
        analysisGets += 1;
        return response(analysisGets === 1 ? initial : { ...initial, revision: 2 });
      }
      if (url.endsWith("/imports/batch-1/analysis/continue") && init?.method === "POST") {
        actionRevisions.push(JSON.parse(String(init.body)).revision);
        return response({ detail: { code: "IMPORT_ANALYSIS_REVISION_CONFLICT" } }, 409);
      }
      return undefined;
    },
  });
  renderApp();

  await userEvent.click(await screen.findByRole("button", { name: "查看导入分析" }));
  expect(analysisGets).toBe(1);
  await userEvent.click(await screen.findByRole("button", { name: actionName }));
  expect(await screen.findByText(/导入分析操作失败：IMPORT_ANALYSIS_REVISION_CONFLICT/)).toBeVisible();
  await waitFor(() => expect(analysisGets).toBeGreaterThanOrEqual(2));
  await userEvent.click(await screen.findByRole("button", { name: actionName }));
  expect(actionRevisions).toEqual([1, 2]);
});

it("serializes candidate mutations globally, shows the current operation and recovers from an ordinary error", async () => {
  const first = deferred<Response>();
  let confirmCalls = 0;
  const secondCandidate: ImportMemoryCandidate = {
    ...pendingCandidate,
    id: "candidate-3",
    kind: "entity",
    payload: { name: "顾遥", kind: "character" },
    evidence: [{ quote: "顾遥留在城门", start: 40, end: 46, relative_path: "人物.md" }],
  };
  appFetch({
    candidates: [pendingCandidate, secondCandidate],
    onRequest: (url, init) => {
      if (url.endsWith("/memory-candidates/candidate-2/confirm") && init?.method === "POST") {
        confirmCalls += 1;
        return first.promise;
      }
      if (url.endsWith("/memory-candidates/candidate-3/confirm") && init?.method === "POST") {
        confirmCalls += 1;
        return response({ ...secondCandidate, status: "confirmed", revision: 6 });
      }
      return undefined;
    },
  });
  renderApp();

  await userEvent.click(await screen.findByRole("button", { name: "审核待确认记忆" }));
  await userEvent.click(await screen.findByRole("button", { name: "确认世界事实候选" }));
  expect(await screen.findByRole("status", { name: "候选记忆操作" })).toHaveTextContent("正在确认世界事实候选");
  expect(screen.getByRole("button", { name: "编辑人物候选" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "确认人物候选" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "拒绝人物候选" })).toBeDisabled();
  expect(screen.getByLabelText("选择人物候选")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "确认人物候选" }));
  expect(confirmCalls).toBe(1);

  await act(async () => { first.resolve(response({ detail: { code: "CANDIDATE_WRITE_FAILED" } }, 500)); });
  const review = screen.getByRole("region", { name: "待确认记忆" });
  expect(await within(review).findByRole("alert")).toHaveTextContent("CANDIDATE_WRITE_FAILED");
  expect(screen.getByRole("button", { name: "确认人物候选" })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: "确认人物候选" }));
  await waitFor(() => expect(confirmCalls).toBe(2));
});

it("polls every two seconds only while analyzing and stops on a terminal state", async () => {
  vi.useFakeTimers();
  const running = { ...analysis, status: "analyzing" as const, last_error: "" };
  const apiState = appFetch({ analyses: [running, { ...running, status: "analyzed" }] });
  const view = renderApp();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(apiState.analysisCalls).toBe(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
  expect(apiState.analysisCalls).toBe(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
  expect(apiState.analysisCalls).toBe(2);
  view.unmount();
});

it("refreshes the pending count when analysis progress produces new candidates", async () => {
  vi.useFakeTimers();
  let analysisRound = 0;
  const running = { ...analysis, status: "analyzing" as const, last_error: "" };
  appFetch({
    onRequest: (url) => {
      if (url.endsWith("/imports/latest/analysis")) {
        analysisRound += 1;
        return response({ ...running, progress: { ...running.progress, completed: analysisRound } });
      }
      if (url.includes("/memory-candidates?")) {
        const items = analysisRound > 1 ? [pendingCandidate] : [];
        return response({ items, total: items.length, counts: { pending: items.length }, next_cursor: null });
      }
      return undefined;
    },
  });
  const view = renderApp();
  await act(async () => { await vi.advanceTimersByTimeAsync(20); });
  expect(screen.getByRole("button", { name: "查看导入分析" })).toBeVisible();
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
  expect(analysisRound).toBe(2);
  expect(screen.getByRole("button", { name: "审核待确认记忆" })).toHaveTextContent("待确认记忆 1");
  view.unmount();
});

it("polls a lightweight candidate count without refetching loaded review pages", async () => {
  vi.useFakeTimers();
  let analysisRound = 0;
  let countRequests = 0;
  let reviewRequests = 0;
  const running = { ...analysis, status: "analyzing" as const, last_error: "" };
  appFetch({
    candidates: [],
    onRequest: (url) => {
      if (url.endsWith("/imports/latest/analysis")) {
        analysisRound += 1;
        return response({ ...running, progress: { ...running.progress, completed: analysisRound } });
      }
      if (url.includes("/memory-candidates?")) {
        const params = new URL(url, "http://local").searchParams;
        const limit = params.get("limit");
        if (limit === "1") {
          countRequests += 1;
          return response({ items: [], total: 0, counts: { pending: analysisRound, conflict: 0 }, next_cursor: null });
        }
        reviewRequests += 1;
        const cursor = params.get("cursor");
        const status = params.get("status");
        if (status === "pending") return response({
          items: cursor ? [{ ...pendingCandidate, id: "candidate-page-2" }] : [pendingCandidate],
          total: 2,
          counts: { pending: 2, conflict: 0 },
          next_cursor: cursor ? null : "page-2",
        });
        return response({ items: [], total: 0, counts: { pending: 2, conflict: 0 }, next_cursor: null });
      }
      return undefined;
    },
  });
  const view = renderApp();
  await act(async () => { await vi.advanceTimersByTimeAsync(20); });
  fireEvent.click(screen.getByRole("button", { name: "审核待确认记忆" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(20); });
  fireEvent.click(screen.getByRole("button", { name: "加载更多待确认记忆" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(20); });
  const reviewRequestsBeforePoll = reviewRequests;
  const countRequestsBeforePoll = countRequests;

  await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
  expect(analysisRound).toBe(2);
  expect(countRequests).toBe(countRequestsBeforePoll + 1);
  expect(reviewRequests).toBe(reviewRequestsBeforePoll);
  view.unmount();
});

it("stops import analysis polling when the project studio unmounts", async () => {
  vi.useFakeTimers();
  const running = { ...analysis, status: "analyzing" as const, last_error: "" };
  const apiState = appFetch({ analyses: [running] });
  const view = renderApp();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(apiState.analysisCalls).toBe(1);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
  expect(apiState.analysisCalls).toBe(1);
});
