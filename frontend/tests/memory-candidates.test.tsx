import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "../src/app/App";
import { ChapterSummaryPanel } from "../src/features/writing/ChapterSummaryPanel";
import { projectApi } from "../src/lib/api";
import type { ChapterSummary, MemoryCandidate } from "../src/lib/types";
import { emptyLibraryPage, emptyWorkspaceView } from "./library-fixtures";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

const summary: ChapterSummary = {
  id: "summary-1",
  chapter_id: "chapter-1",
  version_id: "version-1",
  title: "第一章",
  recap: "她收起铜钥匙。",
  details: {},
  origin: "ai_generated",
  provider: "demo",
  status: "valid",
  revision: 2,
  content_hash: "hash",
};

const canon: MemoryCandidate = {
  origin: "generated",
  id: "candidate-canon",
  project_id: "project-1",
  chapter_id: "chapter-1",
  source_job_id: "job-1",
  source_summary_id: "summary-1",
  source_version_id: "version-1",
  kind: "canon",
  payload: { subject_entity_id: "hero", predicate: "owns", value: "铜钥匙" },
  evidence: { quote: "她收起铜钥匙", start: 0, end: 7 },
  status: "pending",
  revision: 3,
  promoted_record_id: null,
  identity_hash: "a".repeat(64),
  promotion_fingerprint: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const entityState: MemoryCandidate = {
  origin: "generated",
  id: "candidate-state",
  project_id: "project-1",
  chapter_id: "chapter-1",
  source_job_id: "job-1",
  source_summary_id: "summary-1",
  source_version_id: "version-1",
  kind: "entity_state",
  payload: {
    entity_id: "hero",
    data: { location: "钟楼" },
    valid_from_node_id: "chapter-1",
  },
  evidence: { quote: "她走进钟楼", start: 8, end: 14 },
  status: "pending",
  revision: 1,
  promoted_record_id: null,
  identity_hash: "b".repeat(64),
  promotion_fingerprint: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

function renderPanel(overrides: Record<string, unknown> = {}) {
  const props = {
    summary,
    onSave: vi.fn().mockResolvedValue(undefined),
    candidates: [canon, entityState],
    candidatesLoading: false,
    candidateError: "",
    candidateBusyId: null,
    hasMoreCandidates: false,
    loadingMoreCandidates: false,
    onLoadMoreCandidates: vi.fn(),
    onRetryCandidates: vi.fn(),
    onEditCandidate: vi.fn().mockResolvedValue(undefined),
    onConfirmCandidate: vi.fn().mockResolvedValue(undefined),
    onRejectCandidate: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(<ChapterSummaryPanel {...props} />);
  return props;
}

it("shows typed pending memory with Unicode-code-point evidence and saves edits using its revision", async () => {
  const edit = vi.fn().mockRejectedValueOnce(new Error("修订冲突"));
  renderPanel({ onEditCandidate: edit });

  expect(screen.getByRole("heading", { name: "待确认故事记忆" })).toBeVisible();
  expect(screen.getByText("世界事实")).toBeVisible();
  expect(screen.getByText("人物状态")).toBeVisible();
  expect(screen.getByText("她收起铜钥匙")).toBeVisible();
  expect(screen.getByText(/Unicode 码点.*0–7/)).toBeVisible();
  expect(screen.getByText(/"predicate": "owns"/)).toBeVisible();

  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  const payload = screen.getByLabelText("世界事实候选 payload JSON");
  fireEvent.change(payload, {
    target: { value: JSON.stringify({ subject_entity_id: "hero", predicate: "owns", value: "银钥匙" }) },
  });
  fireEvent.change(screen.getByLabelText("世界事实候选证据原文"), { target: { value: "她收起银钥匙" } });
  fireEvent.change(screen.getByLabelText("世界事实候选证据起点"), { target: { value: "1" } });
  fireEvent.change(screen.getByLabelText("世界事实候选证据终点"), { target: { value: "8" } });
  await userEvent.click(screen.getByRole("button", { name: "保存世界事实候选" }));

  expect(edit).toHaveBeenCalledWith(
    "candidate-canon",
    { subject_entity_id: "hero", predicate: "owns", value: "银钥匙" },
    { quote: "她收起银钥匙", start: 1, end: 8 },
    3,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("修订冲突");
  expect((payload as HTMLTextAreaElement).value).toContain("银钥匙");
});

it("freezes the candidate revision on edit entry and preserves that draft after a stale save fails", async () => {
  const edit = vi.fn().mockRejectedValue(new Error("修订冲突"));
  const props = {
    summary,
    onSave: vi.fn(),
    candidates: [canon],
    onEditCandidate: edit,
    onConfirmCandidate: vi.fn(),
    onRejectCandidate: vi.fn(),
  };
  const view = render(<ChapterSummaryPanel {...props} />);
  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  const payload = screen.getByLabelText("世界事实候选 payload JSON");
  fireEvent.change(payload, { target: { value: JSON.stringify({ predicate: "draft", value: "保留我" }) } });

  view.rerender(<ChapterSummaryPanel {...props} candidates={[{
    ...canon,
    revision: 4,
    payload: { predicate: "server", value: "新数据" },
  }]} />);
  await userEvent.click(screen.getByRole("button", { name: "保存世界事实候选" }));

  expect(edit).toHaveBeenCalledWith(
    canon.id,
    { predicate: "draft", value: "保留我" },
    canon.evidence,
    3,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("候选已在其他请求中更新");
  expect((payload as HTMLTextAreaElement).value).toContain("保留我");
});

it("rebuilds the candidate edit baseline from current props after cancelling", async () => {
  const props = {
    summary,
    onSave: vi.fn(),
    candidates: [canon],
    onEditCandidate: vi.fn(),
    onConfirmCandidate: vi.fn(),
    onRejectCandidate: vi.fn(),
  };
  const view = render(<ChapterSummaryPanel {...props} />);
  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  fireEvent.change(screen.getByLabelText("世界事实候选 payload JSON"), {
    target: { value: JSON.stringify({ predicate: "draft", value: "丢弃我" }) },
  });
  const current = { ...canon, revision: 4, payload: { predicate: "server", value: "当前基线" } };
  view.rerender(<ChapterSummaryPanel {...props} candidates={[current]} />);

  await userEvent.click(screen.getByRole("button", { name: "取消编辑" }));
  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  expect((screen.getByLabelText("世界事实候选 payload JSON") as HTMLTextAreaElement).value)
    .toContain("当前基线");
});

it("keeps every independently in-flight candidate disabled until its own edit finishes", async () => {
  let resolveCanon!: () => void;
  let resolveState!: () => void;
  const edit = vi.fn((id: string) => new Promise<void>((resolve) => {
    if (id === canon.id) resolveCanon = resolve;
    else resolveState = resolve;
  }));
  renderPanel({ onEditCandidate: edit });
  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  await userEvent.click(screen.getByRole("button", { name: "编辑人物状态候选" }));
  await userEvent.click(screen.getByRole("button", { name: "保存世界事实候选" }));
  await userEvent.click(screen.getByRole("button", { name: "保存人物状态候选" }));

  expect(screen.getByRole("button", { name: "保存世界事实候选" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "保存人物状态候选" })).toBeDisabled();

  await act(async () => { resolveState(); await Promise.resolve(); });
  expect(screen.getByRole("button", { name: "保存世界事实候选" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "确认人物状态候选" })).toBeEnabled();

  await act(async () => { resolveCanon(); await Promise.resolve(); });
  expect(screen.getByRole("button", { name: "确认世界事实候选" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "确认人物状态候选" })).toBeEnabled();
});

it("reports candidate draft and request state separately to the summary panel owner", async () => {
  let resolveEdit!: () => void;
  const dirty = vi.fn();
  const pending = vi.fn();
  renderPanel({
    candidates: [canon],
    onDirtyChange: dirty,
    onCandidatePendingChange: pending,
    onEditCandidate: vi.fn(() => new Promise<void>((resolve) => { resolveEdit = resolve; })),
  });
  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  fireEvent.change(screen.getByLabelText("世界事实候选 payload JSON"), {
    target: { value: JSON.stringify({ predicate: "draft", value: "未保存" }) },
  });
  await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

  await userEvent.click(screen.getByRole("button", { name: "保存世界事实候选" }));
  await waitFor(() => expect(pending).toHaveBeenLastCalledWith(true));
  await act(async () => { resolveEdit(); await Promise.resolve(); });
  expect(pending).toHaveBeenLastCalledWith(false);
});

it("rejects an empty evidence offset before invoking the candidate API", async () => {
  const edit = vi.fn();
  renderPanel({ candidates: [canon], onEditCandidate: edit });
  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  fireEvent.change(screen.getByLabelText("世界事实候选证据起点"), { target: { value: "" } });
  await userEvent.click(screen.getByRole("button", { name: "保存世界事实候选" }));

  expect(edit).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("证据原文不能为空");
});

it("requires an explicit rebase after a revision conflict while retaining the edit draft", async () => {
  const edit = vi.fn()
    .mockRejectedValueOnce(new Error("revision_conflict"))
    .mockResolvedValueOnce(undefined);
  const props = {
    summary,
    onSave: vi.fn(),
    candidates: [canon],
    onEditCandidate: edit,
    onConfirmCandidate: vi.fn(),
    onRejectCandidate: vi.fn(),
  };
  const view = render(<ChapterSummaryPanel {...props} />);
  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  const payload = screen.getByLabelText("世界事实候选 payload JSON");
  fireEvent.change(payload, { target: { value: JSON.stringify({ predicate: "draft", value: "保留草稿" }) } });
  await userEvent.click(screen.getByRole("button", { name: "保存世界事实候选" }));

  const current = { ...canon, revision: 4, payload: { predicate: "server", value: "服务器值" } };
  view.rerender(<ChapterSummaryPanel {...props} candidates={[current]} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("候选已在其他请求中更新");
  expect((payload as HTMLTextAreaElement).value).toContain("保留草稿");

  await userEvent.click(screen.getByRole("button", { name: "保留草稿并以当前候选为基线" }));
  expect((payload as HTMLTextAreaElement).value).toContain("保留草稿");
  await userEvent.click(screen.getByRole("button", { name: "保存世界事实候选" }));
  expect(edit.mock.calls.map((call) => call[3])).toEqual([3, 4]);
});

it("requires explicit authoritative-memory confirmation, while reject never asks", async () => {
  const confirmDialog = vi.spyOn(window, "confirm").mockReturnValue(true);
  const confirm = vi.fn().mockResolvedValue(undefined);
  const reject = vi.fn().mockResolvedValue(undefined);
  renderPanel({ onConfirmCandidate: confirm, onRejectCandidate: reject });

  await userEvent.click(screen.getByRole("button", { name: "确认世界事实候选" }));
  expect(confirmDialog).toHaveBeenCalledWith("确认后将更新权威故事记忆，是否继续？");
  expect(confirm).toHaveBeenCalledWith("candidate-canon", 3);

  confirmDialog.mockClear();
  await userEvent.click(screen.getByRole("button", { name: "拒绝人物状态候选" }));
  expect(confirmDialog).not.toHaveBeenCalled();
  expect(reject).toHaveBeenCalledWith("candidate-state", 1);
});

it("explains how to resolve a legacy entity-state transition without discarding the candidate", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const legacyError = Object.assign(new Error("LEGACY_STATE_TRANSITION_REQUIRED"), {
    code: "LEGACY_STATE_TRANSITION_REQUIRED",
  });
  renderPanel({
    candidates: [entityState],
    onConfirmCandidate: vi.fn().mockRejectedValue(legacyError),
  });

  await userEvent.click(screen.getByRole("button", { name: "确认人物状态候选" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/legacy_transition.*baseline.*retire/);
  expect(screen.getByRole("button", { name: "编辑人物状态候选" })).toBeVisible();
});

it("disables all candidate and pagination controls during a mutation and exposes loading, errors, and empty state", async () => {
  const page = vi.fn();
  const { rerender } = render(
    <ChapterSummaryPanel
      summary={summary}
      onSave={vi.fn()}
      candidates={[canon, entityState]}
      candidateBusyId={canon.id}
      hasMoreCandidates
      loadingMoreCandidates={false}
      onLoadMoreCandidates={page}
      onRetryCandidates={vi.fn()}
      onEditCandidate={vi.fn()}
      onConfirmCandidate={vi.fn()}
      onRejectCandidate={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "确认世界事实候选" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "拒绝人物状态候选" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "加载更多待确认记忆" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "加载更多待确认记忆" }));
  expect(page).not.toHaveBeenCalled();

  rerender(
    <ChapterSummaryPanel
      summary={summary}
      onSave={vi.fn()}
      candidates={[]}
      candidatesLoading
      onEditCandidate={vi.fn()}
      onConfirmCandidate={vi.fn()}
      onRejectCandidate={vi.fn()}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("正在读取待确认记忆");

  rerender(
    <ChapterSummaryPanel
      summary={summary}
      onSave={vi.fn()}
      candidates={[]}
      candidatesLoading={false}
      candidateError="候选记忆加载失败"
      onRetryCandidates={vi.fn()}
      onEditCandidate={vi.fn()}
      onConfirmCandidate={vi.fn()}
      onRejectCandidate={vi.fn()}
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent("候选记忆加载失败");
  rerender(
    <ChapterSummaryPanel
      summary={summary}
      onSave={vi.fn()}
      candidates={[]}
      candidatesLoading={false}
      onEditCandidate={vi.fn()}
      onConfirmCandidate={vi.fn()}
      onRejectCandidate={vi.fn()}
    />,
  );
  expect(screen.getByText("当前没有待确认的故事记忆。")).toBeVisible();
});

it("uses the paginated candidate contract and sends revisions for edit, confirm, and reject", async () => {
  const requests: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ url: String(input), init });
    return new Response(JSON.stringify({ items: [], next_cursor: null }), {
      headers: { "Content-Type": "application/json" },
    });
  }));
  const client = projectApi("project / one");

  await client.memoryCandidates("next / cursor", "chapter / one", "pending", 100);
  await client.editMemoryCandidate(canon.id, {
    revision: 3,
    payload: canon.payload,
    evidence: canon.evidence,
  });
  await client.confirmMemoryCandidate(canon.id, 4);
  await client.rejectMemoryCandidate(entityState.id, 1);

  expect(requests[0].url).toContain("/memory-candidates?");
  expect(requests[0].url).toContain("chapter_id=chapter+%2F+one");
  expect(requests[0].url).toContain("status=pending");
  expect(requests[0].url).toContain("limit=100");
  expect(requests[0].url).toContain("cursor=next+%2F+cursor");
  expect(requests.slice(1).map(({ url, init }) => [url, init?.method, JSON.parse(String(init?.body))])).toEqual([
    [expect.stringContaining(`/memory-candidates/${canon.id}`), "PATCH", { payload: canon.payload, evidence: canon.evidence, revision: 3 }],
    [expect.stringContaining(`/memory-candidates/${canon.id}/confirm`), "POST", { revision: 4 }],
    [expect.stringContaining(`/memory-candidates/${entityState.id}/reject`), "POST", { revision: 1 }],
  ]);
});

function integratedStudio() {
  const project = { id: "project-1", title: "Novel", premise: "", genre: "", target_words: 1000, daily_goal: 100, status: "active" };
  const chapter = { id: "chapter-1", kind: "chapter", title: "第一章", status: "drafting", order_index: 1, parent_id: null };
  let candidates = [canon, entityState];
  const candidateGets: string[] = [];
  const candidateMutations: string[] = [];
  const summaryMutations: string[] = [];
  const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const page = emptyLibraryPage(url); if (page) return response(page);
    if (url.endsWith("/projects")) return response([project]);
    if (url.endsWith("/workspace/navigation")) return response({ project, nodes: [chapter] });
    if (url.endsWith("/chapters/chapter-1")) return response({ content: "她收起铜钥匙。她走进钟楼。", contract: {}, revision: 2, status: "drafting" });
    if (url.includes("/versions/page?")) return response({ items: [], next_cursor: null });
    if (url.includes("/ai/jobs/page?")) return response({ items: [], next_cursor: null });
    if (url.endsWith("/summary") && init?.method === "PATCH") {
      summaryMutations.push(url);
      return response({ ...summary, recap: JSON.parse(String(init.body)).recap, revision: 3 });
    }
    if (url.endsWith("/summary")) return response(url.includes("/chapter-2/") ? null : summary);
    if (url.includes("/memory-candidates?")) {
      candidateGets.push(url);
      const cursor = new URL(url, "http://fixture").searchParams.get("cursor");
      return response(cursor ? { items: candidates.filter(item => item.id === entityState.id), next_cursor: null }
        : { items: candidates.filter(item => item.id === canon.id), next_cursor: "page-2" });
    }
    if (url.endsWith(`/memory-candidates/${canon.id}/confirm`)) {
      candidateMutations.push(url);
      candidates = candidates.filter(item => item.id !== canon.id);
      return response({ ...canon, status: "confirmed", revision: 4, promoted_record_id: "fact-1" });
    }
    if (url.endsWith(`/memory-candidates/${entityState.id}/reject`)) {
      candidateMutations.push(url);
      candidates = candidates.filter(item => item.id !== entityState.id);
      return response({ ...entityState, status: "rejected", revision: 2 });
    }
    if (url.endsWith("/settings/model")) return response({ mode: "demo", model: "", base_url: "", has_api_key: false, external_consent: false });
    if (url.endsWith("/rag/health")) return response({ vectors: "disabled", documents: 0 });
    if (url.endsWith("/progress")) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 1, completed_chapters: 0, daily_goal: 100 });
    return response([]);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return { candidateGets, candidateMutations, summaryMutations, client };
}

it("loads every candidate page, removes decided cards, and keeps ordinary summary save isolated", async () => {
  const app = integratedStudio();
  const invalidations = vi.spyOn(app.client, "invalidateQueries");
  vi.spyOn(window, "confirm").mockReturnValue(true);
  await screen.findByLabelText("给 AI 的消息");
  await userEvent.click(screen.getByRole("button", { name: "查看正文" }));
  await userEvent.click(screen.getByText("章节总结与连续性"));

  expect(await screen.findByText("世界事实")).toBeVisible();
  expect(screen.queryByText("人物状态")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "加载更多待确认记忆" }));
  expect(await screen.findByText("人物状态")).toBeVisible();
  expect(app.candidateGets.some(url => url.includes("cursor=page-2"))).toBe(true);

  await userEvent.click(screen.getByRole("button", { name: "确认世界事实候选" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "确认世界事实候选" })).not.toBeInTheDocument());
  expect(app.candidateMutations).toEqual([expect.stringContaining("/confirm")]);
  for (const queryKey of [
    ["memory-candidates", "project-1", "chapter-1"],
    ["workspace", "project-1"],
    ["library", "project-1"],
    ["context", "project-1"],
    ["conflicts", "project-1"],
  ]) {
    expect(invalidations).toHaveBeenCalledWith(expect.objectContaining({ queryKey }));
  }

  await userEvent.click(screen.getByRole("button", { name: "拒绝人物状态候选" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "拒绝人物状态候选" })).not.toBeInTheDocument());
  expect(app.candidateMutations).toHaveLength(2);

  await userEvent.click(screen.getByRole("button", { name: "编辑总结" }));
  fireEvent.change(screen.getByLabelText("章节总结"), { target: { value: "作者修订" } });
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  await waitFor(() => expect(app.summaryMutations).toHaveLength(1));
  expect(app.candidateMutations).toHaveLength(2);
});

function reviewStudio(options: { conflictAction?: "confirm" | "reject"; deferred?: boolean; concurrent?: boolean } = {}) {
  const project = { id: "project-1", title: "Novel", premise: "", genre: "", target_words: 1000, daily_goal: 100, status: "active" };
  const otherProject = { ...project, id: "project-2", title: "Other" };
  const chapter1 = { id: "chapter-1", kind: "chapter", title: "第一章", status: "drafting", order_index: 1, parent_id: null };
  const chapter2 = { ...chapter1, id: "chapter-2", title: "第二章", order_index: 2 };
  let currentCandidates: MemoryCandidate[] = options.concurrent ? [canon, entityState] : [canon];
  let conflicted = false;
  let releaseMutation!: () => void;
  let mutationStarted!: () => void;
  const started = new Promise<void>((resolve) => { mutationStarted = resolve; });
  const gate = new Promise<void>((resolve) => { releaseMutation = resolve; });
  let releaseStateMutation!: () => void;
  let stateMutationStarted!: () => void;
  const stateStarted = new Promise<void>((resolve) => { stateMutationStarted = resolve; });
  const stateGate = new Promise<void>((resolve) => { releaseStateMutation = resolve; });
  const candidateGets: string[] = [];
  const decisionBodies: Array<{ action: string; revision: number }> = [];
  const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const page = emptyLibraryPage(url); if (page) return response(page);
    const view = emptyWorkspaceView(url, [chapter1, chapter2]); if (view) return response(view);
    if (url.endsWith("/projects")) return response([project, otherProject]);
    if (url.includes("/projects/project-2/workspace/navigation")) return response({ project: otherProject, nodes: [] });
    if (url.endsWith("/workspace/navigation")) return response({ project, nodes: [chapter1, chapter2] });
    if (url.includes("/versions/page?")) return response({ items: [], next_cursor: null });
    if (url.includes("/chapters/chapter-" ) && !url.endsWith("/summary")) {
      return response({ content: "她收起铜钥匙。", contract: {}, revision: 2, status: "drafting" });
    }
    if (url.includes("/ai/jobs/page?")) return response({ items: [], next_cursor: null });
    if (url.endsWith("/summary")) return response(url.includes("/chapter-2/") ? null : summary);
    if (url.includes("/memory-candidates?")) {
      candidateGets.push(url);
      const chapterId = new URL(url, "http://fixture").searchParams.get("chapter_id");
      return response({ items: chapterId === chapter1.id ? currentCandidates : [], next_cursor: null });
    }
    const decisionMatch = url.match(/\/memory-candidates\/(candidate-(?:canon|state))\/(confirm|reject)$/);
    if (decisionMatch) {
      const [, candidateId, decision] = decisionMatch;
      const body = JSON.parse(String(init?.body));
      decisionBodies.push({ action: decision, revision: body.revision });
      if (candidateId === canon.id) mutationStarted();
      else stateMutationStarted();
      if (options.deferred) await (candidateId === canon.id ? gate : stateGate);
      if (candidateId === canon.id && options.conflictAction === decision && !conflicted) {
        conflicted = true;
        const currentCandidate = { ...canon, revision: 4 };
        currentCandidates = currentCandidates.map(candidate => candidate.id === canon.id ? currentCandidate : candidate);
        return response({ detail: { code: "revision_conflict", current: currentCandidate } }, 409);
      }
      const currentCandidate = currentCandidates.find(candidate => candidate.id === candidateId)!;
      const result = { ...currentCandidate, status: decision === "confirm" ? "confirmed" : "rejected", revision: body.revision + 1 };
      currentCandidates = currentCandidates.filter(candidate => candidate.id !== candidateId);
      return response(result);
    }
    if (url.endsWith("/settings/model")) return response({ mode: "demo", model: "", base_url: "", has_api_key: false, external_consent: false });
    if (url.endsWith("/rag/health")) return response({ vectors: "disabled", documents: 0 });
    if (url.endsWith("/progress")) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 2, completed_chapters: 0, daily_goal: 100 });
    return response([]);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return {
    candidateGets,
    decisionBodies,
    client,
    project,
    chapters: [chapter1, chapter2],
    started,
    stateStarted,
    release: async () => { await act(async () => { releaseMutation(); await Promise.resolve(); }); },
    releaseState: async () => { await act(async () => { releaseStateMutation(); await Promise.resolve(); }); },
  };
}

async function openCandidatePanel() {
  await screen.findByLabelText("给 AI 的消息");
  await userEvent.click(screen.getByRole("button", { name: "查看正文" }));
  await userEvent.click(screen.getByText("章节总结与连续性"));
  await screen.findByRole("button", { name: "编辑世界事实候选" });
}

it("uses existing dirty and beforeunload guards for an edited memory candidate", async () => {
  reviewStudio();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await openCandidatePanel();
  await userEvent.click(screen.getByRole("button", { name: "编辑世界事实候选" }));
  fireEvent.change(screen.getByLabelText("世界事实候选 payload JSON"), {
    target: { value: JSON.stringify({ predicate: "draft", value: "未保存" }) },
  });

  const leave = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(leave);
  expect(leave.defaultPrevented).toBe(true);
  await userEvent.click(screen.getByRole("button", { name: "第二章" }));
  expect(confirm).toHaveBeenCalledWith("本章有未保存修改。确定切换章节？");
  expect(screen.getByRole("button", { name: "第一章" })).toHaveAttribute("aria-current", "page");
});

it("hard-blocks chapter, project, workspace, and browser departure while a candidate request is pending", async () => {
  const app = reviewStudio({ deferred: true });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  await openCandidatePanel();
  await userEvent.click(screen.getByRole("button", { name: "确认世界事实候选" }));
  await app.started;
  confirm.mockClear();

  const leave = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(leave);
  expect(leave.defaultPrevented).toBe(true);
  await userEvent.click(screen.getByRole("button", { name: "第二章" }));
  expect(await screen.findByText("请等待当前请求结束后再切换章节。")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  expect(await screen.findByText("请等待当前请求结束后再切换工作区。")).toBeVisible();
  await userEvent.selectOptions(screen.getByLabelText("当前小说项目"), "project-2");
  expect(screen.getByLabelText("当前小说项目")).toHaveValue("project-1");
  expect(await screen.findByText("请等待当前请求结束后再切换小说。")).toBeVisible();
  expect(confirm).not.toHaveBeenCalled();
  await app.release();
});

it.each(["confirm", "reject"] as const)("refetches the source scope after a %s revision conflict and retries with current revision", async action => {
  const app = reviewStudio({ conflictAction: action });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  await openCandidatePanel();
  const label = action === "confirm" ? "确认世界事实候选" : "拒绝世界事实候选";
  await userEvent.click(screen.getByRole("button", { name: label }));

  await waitFor(() => expect(app.candidateGets.length).toBeGreaterThanOrEqual(2));
  expect(screen.getByRole("button", { name: label })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: label }));
  await waitFor(() => expect(app.decisionBodies).toHaveLength(2));
  expect(app.decisionBodies.map(item => item.revision)).toEqual([3, 4]);
});

it("invalidates only the mutation source chapter when the visible chapter changes before completion", async () => {
  const app = reviewStudio({ deferred: true });
  const invalidations = vi.spyOn(app.client, "invalidateQueries");
  vi.spyOn(window, "confirm").mockReturnValue(true);
  await openCandidatePanel();
  await userEvent.click(screen.getByRole("button", { name: "确认世界事实候选" }));
  await app.started;
  invalidations.mockClear();
  act(() => {
    app.client.setQueryData(["workspace", "project-1", "navigation"], {
      project: app.project,
      nodes: [app.chapters[1]],
    });
  });
  await waitFor(() => expect(screen.queryByRole("button", { name: "确认世界事实候选" })).not.toBeInTheDocument());
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  expect(await screen.findByText("请等待当前请求结束后再切换工作区。")).toBeVisible();
  await app.release();
  await waitFor(() => expect(invalidations).toHaveBeenCalledWith(expect.objectContaining({
    queryKey: ["memory-candidates", "project-1", "chapter-1"],
  })));
  expect(invalidations).not.toHaveBeenCalledWith(expect.objectContaining({
    queryKey: ["memory-candidates", "project-1", "chapter-2"],
  }));
  await waitFor(() => expect(app.client.isMutating()).toBe(0));
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  expect(await screen.findByRole("heading", { name: "项目素材库" })).toBeVisible();
});

it("serializes candidate requests globally and unlocks the next candidate after settlement", async () => {
  const app = reviewStudio({ deferred: true, concurrent: true });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  await openCandidatePanel();
  await userEvent.click(screen.getByRole("button", { name: "确认世界事实候选" }));
  await app.started;
  await waitFor(() => expect(app.client.isMutating()).toBe(1));
  expect(screen.getByRole("button", { name: "确认人物状态候选" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "确认人物状态候选" }));
  expect(app.decisionBodies).toHaveLength(1);

  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  expect(await screen.findByText("请等待当前请求结束后再切换工作区。")).toBeVisible();
  await app.release();
  await waitFor(() => expect(app.client.isMutating()).toBe(0));
  expect(screen.getByRole("button", { name: "确认人物状态候选" })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: "确认人物状态候选" }));
  await app.stateStarted;
  expect(app.decisionBodies).toHaveLength(2);
  await app.releaseState();
});
