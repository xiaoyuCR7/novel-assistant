import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { BookImportWizard } from "../src/features/projects/BookImportWizard";
import { ProjectLauncher } from "../src/features/projects/ProjectLauncher";
import { App } from "../src/app/App";
import { Modal } from "../src/components/Modal";
import { ApiError, api, projectApi } from "../src/lib/api";
import type {
  BoundedJson,
  ImportAnalysisAction,
  ImportAnalysisStatus,
  ImportAnalysisUnit,
  ImportBatch,
  ImportCommitRequest,
  ImportCommitResponse,
  ImportDraft,
  ImportDraftPatch,
  ImportLimits,
  MemoryCandidate,
  MemoryCandidateBulkConfirmRequest,
  MemoryCandidateDecisionRequest,
  MemoryCandidatePatchRequest,
} from "../src/lib/types";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

const draft: ImportDraft = {
  draft_id: "draft / one",
  title: "旧城",
  source_kind: "zip",
  manifest: {},
  files: [{
    relative_path: "正文/第一章.md", audit_id: "source-1", category: "manuscript",
    title: "第一章", encoding: "utf-8", size_bytes: 12, byte_hash: "b", content_hash: "c",
    selected: true, warning: "", content_preview: "",
  }],
  chapters: [{
    source_document_id: "source-1", draft_chapter_id: "chapter-1", relative_path: "正文/第一章.md",
    title: "第一章", order_index: 0, existing_chapter_id: null, content_preview: "",
  }],
  continuation: {
    confirmed: false, completed_through_node_id: null, current_chapter_id: null,
    objective: "", source_document_ids: [], revision: 1,
  },
  objective: "",
  revision: 1,
};
const effectiveImportLimits: ImportLimits = {
  max_files: 12,
  max_file_bytes: 2 * 1024 * 1024,
  max_total_bytes: 24 * 1024 * 1024,
  max_compression_ratio: 8,
};

const boundedJson: BoundedJson = { nested: ["text", 3, 1.5, true, null] };
const batch: ImportBatch = {
  id: "batch-1", project_id: "project-1", source_kind: "zip", manifest: boundedJson,
  status: "analyzing", completed_units: 1, total_units: 2, last_error: "", revision: 3,
  created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:01:00Z",
};
const analysisUnit: ImportAnalysisUnit = {
  id: "unit-1", project_id: "project-1", import_batch_id: "batch-1",
  source_document_id: "source-1", chapter_id: null, chunk_key: "chunk:0", unit_key: "source:0",
  kind: "source_memory", source_hash: "a".repeat(64), status: "running", attempt_count: 1,
  worker_epoch: "epoch-1", error_code: null, error_message: "", result: {},
  created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:01:00Z",
};
const analysisStatus: ImportAnalysisStatus = {
  id: batch.id, project_id: batch.project_id, status: batch.status, revision: batch.revision,
  progress: { total: 2, completed: 1, failed: 0, queued: 0, running: 1 },
  current_unit: analysisUnit, last_error: "", created_at: batch.created_at, updated_at: batch.updated_at,
};
const importCandidate: MemoryCandidate = {
  origin: "import", id: "candidate-1", project_id: "project-1", import_batch_id: "batch-1",
  source_document_id: "source-1", chapter_id: null, source_version_id: null, kind: "entity",
  payload: { name: "林渡" }, evidence: [{ quote: "林渡", start: 0, end: 2 }],
  source_hash: "a".repeat(64), dedupe_key: "b".repeat(64), analysis_identity: null,
  status: "pending", revision: 1,
  conflict: {}, promoted_type: null, promoted_record_id: null, promotion_fingerprint: null,
  created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:01:00Z",
};
const commitResponse: ImportCommitResponse = {
  project: {
    id: "project-1", title: "旧城", premise: "", genre: "", target_words: 100_000,
    daily_goal: 1_500, status: "active", created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  import_batch_id: "batch-1",
};
// @ts-expect-error revision-only candidate edits are rejected by the backend contract.
const revisionOnlyCandidatePatch: MemoryCandidatePatchRequest = { revision: 1 };
const requestContracts: [
  ImportDraftPatch,
  ImportCommitRequest,
  ImportAnalysisAction,
  MemoryCandidatePatchRequest,
  MemoryCandidateDecisionRequest,
  MemoryCandidateBulkConfirmRequest,
] = [
  { revision: 1, objective: "续写" },
  { expected_revision: 1 },
  { revision: 1, adopt_current_provider: true },
  { revision: 1, payload: { name: "林渡" } },
  { revision: 1, decision: "confirm", resolution: "create_separate" },
  { entries: [{ candidate_id: "candidate-1", revision: 1 }] },
];

async function advanceImportWizardToCommit() {
  fireEvent.change(screen.getByLabelText("ZIP 文件"), {
    target: { files: [new File(["正文"], "旧城.zip", { type: "application/zip" })] },
  });
  await userEvent.click(screen.getByRole("button", { name: "下一步：检查章节" }));
  await screen.findAllByText("正文/第一章.md");
  await userEvent.click(screen.getByRole("button", { name: "下一步：确认续写点" }));
  await userEvent.click(screen.getByLabelText("我已确认续写边界"));
  await userEvent.click(screen.getByRole("button", { name: "下一步：创建项目" }));
}

it("keeps import response and mutation contracts structurally typed", () => {
  expect(analysisStatus.current_unit?.attempt_count).toBe(1);
  expect(importCandidate.origin).toBe("import");
  expect(commitResponse.project.created_at).toContain("2026-09-01");
  expect(revisionOnlyCandidatePatch.revision).toBe(1);
  expect(requestContracts).toHaveLength(6);
});

it("requests the current effective import limits through the global import API", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(effectiveImportLimits), {
    headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(api.importLimits()).resolves.toEqual(effectiveImportLimits);

  expect(fetchMock).toHaveBeenCalledWith("/api/v1/imports/limits", expect.any(Object));
});

it("uploads multipart data without forcing a JSON content type", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(draft), {
    status: 201, headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const form = new FormData();
  form.set("source_kind", "zip");
  form.set("display_name", "旧城");
  form.append("files", new File(["正文"], "旧城.zip", { type: "application/zip" }));

  await api.createImportDraft(form);

  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/v1/imports");
  expect(init.method).toBe("POST");
  expect(init.body).toBe(form);
  expect(init.headers).toBeUndefined();
});

it("bulk-confirms import memory candidates with their expected revisions", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }), {
    headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const client = projectApi("project / 旧城");

  await client.bulkConfirmMemoryCandidates([
    { candidate_id: "candidate / 林渡", revision: 3 },
  ]);

  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/v1/projects/project%20%2F%20%E6%97%A7%E5%9F%8E/memory-candidates/bulk-confirm");
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({ "Content-Type": "application/json" });
  expect(JSON.parse(String(init.body))).toEqual({
    entries: [{ candidate_id: "candidate / 林渡", revision: 3 }],
  });
});

it("preserves upload request options without adding a multipart content type", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(draft), {
    status: 201, headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const form = new FormData();
  form.set("source_kind", "zip");
  const controller = new AbortController();

  await api.createImportDraft(form, {
    credentials: "include",
    headers: { "X-Trace": "trace-1", "cOnTeNt-TyPe": "application/json" },
    signal: controller.signal,
  });

  expect(fetchMock).toHaveBeenCalledWith("/api/v1/imports", expect.objectContaining({
    method: "POST",
    body: form,
    credentials: "include",
    headers: { "X-Trace": "trace-1" },
    signal: controller.signal,
  }));
});

it("removes multipart content type from Headers and tuple inputs", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify(draft), {
    status: 201, headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const form = new FormData();
  const headerInputs: HeadersInit[] = [
    new Headers({ "Content-Type": "text/plain", "X-Headers": "kept" }),
    [["CONTENT-TYPE", "application/json"], ["X-Tuple", "kept"]],
  ];

  for (const headers of headerInputs) await api.createImportDraft(form, { headers });

  const first = new Headers(fetchMock.mock.calls[0][1].headers);
  const second = new Headers(fetchMock.mock.calls[1][1].headers);
  expect(first.has("Content-Type")).toBe(false);
  expect(first.get("X-Headers")).toBe("kept");
  expect(second.has("Content-Type")).toBe(false);
  expect(second.get("X-Tuple")).toBe("kept");
});

it("encodes every global import identifier and sends exact revision keys as JSON", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify(draft), {
    headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const encoded = "draft%20%2F%20%E6%97%A7%E5%9F%8E";
  await api.getImportDraft("draft / 旧城");
  await api.patchImportDraft("draft / 旧城", { revision: 7, objective: "续写" });
  await api.commitImportDraft("draft / 旧城", 8);
  await api.discardImportDraft("draft / 旧城");

  expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
    `/api/v1/imports/${encoded}`,
    `/api/v1/imports/${encoded}`,
    `/api/v1/imports/${encoded}/commit`,
    `/api/v1/imports/${encoded}`,
  ]);
  expect(fetchMock.mock.calls[1][1]).toMatchObject({
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision: 7, objective: "续写" }),
  });
  expect(fetchMock.mock.calls[2][1]).toMatchObject({
    method: "POST",
    body: JSON.stringify({ expected_revision: 8 }),
  });
});

it("encodes project-local identifiers and sends analysis provider adoption exactly", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({}), {
    headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const client = projectApi("project / 旧城");

  await client.importAnalysis("batch / 一");
  await client.pauseImportAnalysis("batch / 一", 2);
  await client.continueImportAnalysis("batch / 一", 3, false);
  await client.retryImportAnalysis("batch / 一", 4, true);
  await client.editMemoryCandidate("candidate / 林渡", {
    revision: 5,
    payload: { name: "林渡" },
  });
  await client.confirmMemoryCandidate("candidate / 林渡", 6, "create_separate");
  await client.rejectMemoryCandidate("candidate / 林渡", 7);

  const urls = fetchMock.mock.calls.map(call => String(call[0]));
  const prefix = "/api/v1/projects/project%20%2F%20%E6%97%A7%E5%9F%8E";
  expect(urls).toEqual([
    `${prefix}/imports/batch%20%2F%20%E4%B8%80/analysis`,
    `${prefix}/imports/batch%20%2F%20%E4%B8%80/analysis/pause`,
    `${prefix}/imports/batch%20%2F%20%E4%B8%80/analysis/continue`,
    `${prefix}/imports/batch%20%2F%20%E4%B8%80/analysis/retry`,
    `${prefix}/memory-candidates/candidate%20%2F%20%E6%9E%97%E6%B8%A1`,
    `${prefix}/memory-candidates/candidate%20%2F%20%E6%9E%97%E6%B8%A1/confirm`,
    `${prefix}/memory-candidates/candidate%20%2F%20%E6%9E%97%E6%B8%A1/reject`,
  ]);
  expect(JSON.parse(String(fetchMock.mock.calls[2][1].body))).toEqual({
    revision: 3, adopt_current_provider: false,
  });
  expect(JSON.parse(String(fetchMock.mock.calls[3][1].body))).toEqual({
    revision: 4, adopt_current_provider: true,
  });
  expect(JSON.parse(String(fetchMock.mock.calls[4][1].body))).toEqual({
    revision: 5, payload: { name: "林渡" },
  });
});

it("sends kind-only and evidence-only candidate patches without invented fields", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({}), {
    headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const client = projectApi("project-1");

  await client.editMemoryCandidate("candidate-kind", { revision: 2, kind: "entity" });
  await client.editMemoryCandidate("candidate-evidence", {
    revision: 3,
    evidence: [{ quote: "林渡", start: 0, end: 2 }],
  });

  expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({ revision: 2, kind: "entity" });
  expect(JSON.parse(String(fetchMock.mock.calls[1][1].body))).toEqual({
    revision: 3,
    evidence: [{ quote: "林渡", start: 0, end: 2 }],
  });
});

it("encodes a memory cursor once and omits absent filters", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({
    items: [], total: 0, counts: {}, next_cursor: null,
  }), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  const client = projectApi("p");
  const cursor = "+/=雪?&";

  await client.memoryCandidates(cursor, "chapter / 一", "conflict", 37);
  await client.memoryCandidates(undefined, undefined, null, 50);
  await client.memoryCandidates(undefined, undefined, "pending", 100, "import");

  expect(fetchMock.mock.calls[0][0]).toBe(
    "/api/v1/projects/p/memory-candidates?limit=37&status=conflict&chapter_id=chapter+%2F+%E4%B8%80&cursor=%2B%2F%3D%E9%9B%AA%3F%26",
  );
  expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/projects/p/memory-candidates?limit=50");
  expect(fetchMock.mock.calls[2][0]).toBe(
    "/api/v1/projects/p/memory-candidates?limit=100&status=pending&origin=import",
  );
  expect(String(fetchMock.mock.calls[1][0])).not.toContain("undefined");
});

it("normalizes JSON and text errors and accepts empty 204 responses", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({
      detail: { code: "revision_conflict", message: "版本冲突", current: { revision: 9 }, job_id: "job-1" },
    }), { status: 409, headers: { "Content-Type": "application/json" } }))
    .mockResolvedValueOnce(new Response("upstream unavailable", { status: 502 }))
    .mockResolvedValueOnce(new Response(null, { status: 503 }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(api.projects()).rejects.toMatchObject({
    status: 409,
    message: "版本冲突",
    code: "revision_conflict",
    current: { revision: 9 },
    jobId: "job-1",
  } satisfies Partial<ApiError>);
  await expect(api.projects()).rejects.toMatchObject({
    status: 502,
    message: "upstream unavailable",
  } satisfies Partial<ApiError>);
  await expect(api.projects()).rejects.toMatchObject({
    status: 503,
    message: "请求失败 (503)",
  } satisfies Partial<ApiError>);
  await expect(api.discardImportDraft("draft / done")).resolves.toBeUndefined();
});

it("offers the import workflow beside blank-project creation", async () => {
  const onImport = vi.fn();
  render(<ProjectLauncher onCreate={vi.fn()} onImport={onImport} busy={false} />);
  const blankAction = screen.getByRole("button", { name: "新建空白小说" });
  const importAction = screen.getByRole("button", { name: "导入已有小说" });
  expect(blankAction.parentElement).toContainElement(importAction);
  await userEvent.click(importAction);
  expect(onImport).toHaveBeenCalledOnce();
});

it("opens the import wizard from the empty application launcher", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { headers: { "Content-Type": "application/json" } })));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  await userEvent.click(await screen.findByRole("button", { name: "导入已有小说" }));
  expect(await screen.findByRole("dialog", { name: "导入已有小说" })).toBeVisible();
});

it("opens the shared import wizard from the existing-project creation modal", async () => {
  const project = commitResponse.project;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/projects")) return new Response(JSON.stringify([project]), { headers: { "Content-Type": "application/json" } });
    if (url.endsWith("/workspace/navigation")) return new Response(JSON.stringify({ project, nodes: [] }), { headers: { "Content-Type": "application/json" } });
    return new Promise<Response>(() => undefined);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  await userEvent.click(await screen.findByRole("button", { name: "创建新小说" }));
  expect(screen.getByRole("dialog", { name: "创建独立小说项目" })).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "导入已有小说" }));

  expect(screen.queryByRole("dialog", { name: "创建独立小说项目" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "导入已有小说" })).toBeVisible();
  expect(screen.getAllByLabelText("导入进度")).toHaveLength(1);
});

const existingProject = {
  ...commitResponse.project,
  id: "old-project",
  title: "旧项目",
};

it.each([
  { cacheName: "empty", initialProjects: [] },
  { cacheName: "existing-project", initialProjects: [existingProject] },
])("activates a committed import immediately from an $cacheName cache", async ({ initialProjects }) => {
  let projectReads = 0;
  let revision = 1;
  let serverDraft = structuredClone(draft);
  const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
    status, headers: { "Content-Type": "application/json" },
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/projects")) {
      projectReads += 1;
      if (projectReads === 1) return response(initialProjects);
      return new Promise<Response>(() => undefined);
    }
    if (url === "/api/v1/imports" && init?.method === "POST") return response(draft, 201);
    if (url.endsWith("/commit") && init?.method === "POST") return response(commitResponse, 201);
    if (url.includes("/api/v1/imports/") && init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body));
      revision += 1;
      serverDraft = { ...serverDraft, ...patch, revision };
      return response(serverDraft);
    }
    if (url.endsWith("/workspace/navigation")) {
      const project = url.includes(`/${existingProject.id}/`) ? existingProject : commitResponse.project;
      return response({ project, nodes: [] });
    }
    return new Promise<Response>(() => undefined);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  if (initialProjects.length) {
    await userEvent.click(await screen.findByRole("button", { name: "创建新小说" }));
    await userEvent.click(screen.getByRole("button", { name: "导入已有小说" }));
  } else {
    await userEvent.click(await screen.findByRole("button", { name: "导入已有小说" }));
  }
  await advanceImportWizardToCommit();
  await userEvent.click(screen.getByRole("button", { name: "创建并导入小说" }));

  await waitFor(() => expect(localStorage.getItem("studio:active-project")).toBe("project-1"));
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["projects"] });
  expect(projectReads).toBeGreaterThanOrEqual(2);
  expect(screen.queryByRole("dialog", { name: "导入已有小说" })).not.toBeInTheDocument();
  expect(await screen.findByLabelText("当前小说项目")).toHaveValue("project-1");
  expect(screen.getByLabelText("当前小说项目").querySelector("option:checked")).toHaveTextContent("旧城");
  expect(client.getQueryData(["projects"])).toEqual([
    commitResponse.project,
    ...initialProjects.filter((project) => project.id !== commitResponse.project.id),
  ]);
});

it("discards an uploaded draft when the import modal is closed", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/projects")) {
      return new Response("[]", { headers: { "Content-Type": "application/json" } });
    }
    if (url.endsWith("/imports") && init?.method === "POST") {
      return new Response(JSON.stringify(draft), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    return new Response("{}", { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  await userEvent.click(await screen.findByRole("button", { name: "导入已有小说" }));
  fireEvent.change(screen.getByLabelText("ZIP 文件"), {
    target: { files: [new File(["正文"], "旧城.zip", { type: "application/zip" })] },
  });
  await userEvent.click(screen.getByRole("button", { name: "下一步：检查章节" }));
  await screen.findAllByText("正文/第一章.md");
  await userEvent.click(screen.getByRole("button", { name: "关闭对话框" }));
  await userEvent.click(screen.getByRole("button", { name: "放弃导入" }));

  await waitFor(() => expect(screen.queryByRole("dialog", { name: "导入已有小说" })).not.toBeInTheDocument());
  expect(fetchMock.mock.calls.some(([url, init]) =>
    String(url).includes("/imports/draft%20%2F%20one") && init?.method === "DELETE"
  )).toBe(true);
});

it("waits for an in-flight upload, cleans the returned draft, and keeps a failed cleanup retryable", async () => {
  let resolveUpload!: (value: ImportDraft) => void;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { headers: { "Content-Type": "application/json" } })));
  const create = vi.spyOn(api, "createImportDraft").mockImplementation(() => new Promise((resolve) => {
    resolveUpload = resolve;
  }));
  const discard = vi.spyOn(api, "discardImportDraft")
    .mockRejectedValueOnce(new Error("草稿清理失败"))
    .mockResolvedValueOnce(undefined);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  await userEvent.click(await screen.findByRole("button", { name: "导入已有小说" }));
  fireEvent.change(screen.getByLabelText("ZIP 文件"), {
    target: { files: [new File(["正文"], "旧城.zip", { type: "application/zip" })] },
  });
  await userEvent.click(screen.getByRole("button", { name: "下一步：检查章节" }));
  await userEvent.click(screen.getByRole("button", { name: "关闭对话框" }));

  expect(screen.getByRole("dialog", { name: "导入已有小说" })).toBeVisible();
  await act(async () => resolveUpload(structuredClone(draft)));
  expect(await screen.findByRole("alertdialog", { name: "放弃这次导入？" })).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("必须重试放弃导入后才能关闭");
  expect(discard).toHaveBeenCalledWith("draft / one");

  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("alertdialog", { name: "放弃这次导入？" })).toBeVisible();
  const continueEditing = screen.getByRole("button", { name: "继续编辑" });
  expect(continueEditing).toBeDisabled();
  await userEvent.click(continueEditing);
  expect(screen.getByRole("alertdialog", { name: "放弃这次导入？" })).toBeVisible();
  const hiddenZipInput = document.querySelector<HTMLInputElement>('input[aria-label="ZIP 文件"]')!;
  fireEvent.change(hiddenZipInput, {
    target: { files: [new File(["另一份正文"], "另一本.zip", { type: "application/zip" })] },
  });
  fireEvent.submit(hiddenZipInput.closest("form")!);
  expect(create).toHaveBeenCalledOnce();

  await userEvent.click(screen.getByRole("button", { name: "放弃导入" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "导入已有小说" })).not.toBeInTheDocument());
  expect(discard).toHaveBeenCalledTimes(2);
  expect(discard).toHaveBeenNthCalledWith(2, "draft / one");
});

it.each(["backdrop", "Escape"])("keeps the wizard mounted for an in-flight upload closed by %s", async (closeMethod) => {
  let resolveUpload!: (value: ImportDraft) => void;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { headers: { "Content-Type": "application/json" } })));
  vi.spyOn(api, "createImportDraft").mockImplementation(() => new Promise((resolve) => {
    resolveUpload = resolve;
  }));
  const discard = vi.spyOn(api, "discardImportDraft").mockResolvedValue(undefined);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  await userEvent.click(await screen.findByRole("button", { name: "导入已有小说" }));
  fireEvent.change(screen.getByLabelText("ZIP 文件"), {
    target: { files: [new File(["正文"], "旧城.zip", { type: "application/zip" })] },
  });
  await userEvent.click(screen.getByRole("button", { name: "下一步：检查章节" }));
  if (closeMethod === "backdrop") {
    fireEvent.mouseDown(document.querySelector(".modal-backdrop") as HTMLElement);
  } else {
    fireEvent.keyDown(screen.getByRole("dialog", { name: "导入已有小说" }), { key: "Escape" });
  }

  expect(screen.getByRole("dialog", { name: "导入已有小说" })).toBeVisible();
  await act(async () => resolveUpload(structuredClone(draft)));
  await waitFor(() => expect(discard).toHaveBeenCalledWith("draft / one"));
  expect(screen.queryByRole("dialog", { name: "导入已有小说" })).not.toBeInTheDocument();
});

it("keeps the returned draft locked while automatic DELETE is pending and retries that same id after rejection", async () => {
  let resolveUpload!: (value: ImportDraft) => void;
  let rejectAutomaticDelete!: (cause: Error) => void;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { headers: { "Content-Type": "application/json" } })));
  vi.spyOn(api, "createImportDraft").mockImplementation(() => new Promise((resolve) => {
    resolveUpload = resolve;
  }));
  const discard = vi.spyOn(api, "discardImportDraft")
    .mockImplementationOnce(() => new Promise<void>((_resolve, reject) => {
      rejectAutomaticDelete = reject;
    }))
    .mockResolvedValueOnce(undefined);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  await userEvent.click(await screen.findByRole("button", { name: "导入已有小说" }));
  fireEvent.change(screen.getByLabelText("ZIP 文件"), {
    target: { files: [new File(["正文"], "旧城.zip", { type: "application/zip" })] },
  });
  await userEvent.click(screen.getByRole("button", { name: "下一步：检查章节" }));
  await userEvent.click(screen.getByRole("button", { name: "关闭对话框" }));
  await act(async () => resolveUpload(structuredClone(draft)));

  expect(await screen.findByRole("alertdialog", { name: "放弃这次导入？" })).toHaveFocus();
  expect(screen.getByRole("button", { name: "正在放弃…" })).toBeDisabled();
  fireEvent.mouseDown(document.querySelector(".modal-backdrop") as HTMLElement);
  fireEvent.keyDown(screen.getByRole("dialog", { name: "导入已有小说" }), { key: "Escape" });
  expect(screen.getByRole("dialog", { name: "导入已有小说" })).toBeVisible();

  await act(async () => rejectAutomaticDelete(new Error("自动删除超时")));
  expect(await screen.findByRole("alert")).toHaveTextContent("必须重试放弃导入后才能关闭");
  expect(screen.getByRole("alertdialog", { name: "放弃这次导入？" })).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "放弃导入" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "导入已有小说" })).not.toBeInTheDocument());
  expect(discard.mock.calls).toEqual([["draft / one"], ["draft / one"]]);
});

it("traps focus inside discard confirmation and Escape returns to its trigger without closing the modal", async () => {
  vi.spyOn(api, "createImportDraft").mockResolvedValue(structuredClone(draft));
  const closeModal = vi.fn();
  render(
    <Modal title="导入已有小说" onClose={closeModal}>
      <BookImportWizard onComplete={vi.fn()} onCancel={vi.fn()} />
    </Modal>,
  );
  fireEvent.change(screen.getByLabelText("ZIP 文件"), {
    target: { files: [new File(["正文"], "旧城.zip", { type: "application/zip" })] },
  });
  await userEvent.click(screen.getByRole("button", { name: "下一步：检查章节" }));
  await screen.findByRole("heading", { name: "检查文件与章节" });
  const trigger = screen.getByRole("button", { name: "取消导入" });
  await userEvent.click(trigger);

  expect(screen.getByRole("alertdialog", { name: "放弃这次导入？" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "关闭对话框" })).not.toBeInTheDocument();
  expect(screen.queryByRole("list", { name: "导入进度" })).not.toBeInTheDocument();
  const workspace = document.querySelector(".book-import-workspace") as HTMLElement;
  expect(workspace).toHaveAttribute("aria-hidden", "true");
  expect(workspace.inert).toBe(true);
  expect(screen.getByRole("button", { name: "继续编辑" })).toHaveFocus();
  await userEvent.tab({ shift: true });
  expect(screen.getByRole("button", { name: "放弃导入" })).toHaveFocus();
  await userEvent.tab();
  expect(screen.getByRole("button", { name: "继续编辑" })).toHaveFocus();

  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("alertdialog", { name: "放弃这次导入？" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "导入已有小说" })).toBeVisible();
  expect(closeModal).not.toHaveBeenCalled();
  expect(trigger).toHaveFocus();
});

it("keeps focus on the alertdialog while its only DELETE action is pending", async () => {
  let resolveDiscard!: () => void;
  vi.spyOn(api, "discardImportDraft").mockImplementation(() => new Promise<void>((resolve) => {
    resolveDiscard = resolve;
  }));
  await uploadFolderAndOpenReview();
  await userEvent.click(screen.getByRole("button", { name: "取消导入" }));
  await userEvent.click(screen.getByRole("button", { name: "放弃导入" }));

  const dialog = screen.getByRole("alertdialog", { name: "放弃这次导入？" });
  await waitFor(() => expect(dialog).toHaveFocus());
  expect(dialog).toHaveAttribute("tabindex", "-1");
  expect(screen.getByRole("button", { name: "继续编辑" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "正在放弃…" })).toBeDisabled();
  await userEvent.tab();
  expect(dialog).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("alertdialog", { name: "放弃这次导入？" })).toBeVisible();
  expect(dialog).toHaveFocus();

  await act(async () => resolveDiscard());
});

it("uploads, reviews, confirms the continuation point, and commits the imported project", async () => {
  const requests: Array<{ url: string; init?: RequestInit }> = [];
  let revision = 1;
  let serverDraft = structuredClone(draft);
  const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
    status, headers: { "Content-Type": "application/json" },
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, init });
    if (url === "/api/v1/imports") return response(draft, 201);
    if (url.endsWith("/commit")) return response({
      project: {
        id: "project-1", title: "旧城", premise: "", genre: "", target_words: 100000,
        daily_goal: 1500, status: "active", created_at: "2026-09-01T00:00:00Z",
        updated_at: "2026-09-01T00:00:00Z",
      },
      import_batch_id: "batch-1",
    }, 201);
    if (init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      revision += 1;
      serverDraft = { ...serverDraft, ...body, revision };
      return response(serverDraft);
    }
    return response({});
  }));
  const onComplete = vi.fn();
  const beforeCommit = vi.fn(() => true);
  render(<BookImportWizard beforeCommit={beforeCommit} onComplete={onComplete} onCancel={vi.fn()} />);

  await advanceImportWizardToCommit();
  await userEvent.click(screen.getByRole("button", { name: "创建并导入小说" }));

  await waitFor(() => expect(onComplete).toHaveBeenCalledWith(expect.objectContaining({ import_batch_id: "batch-1" })));
  expect(beforeCommit).toHaveBeenCalledOnce();
  expect(requests.filter(item => item.init?.method === "PATCH")).toHaveLength(3);
  expect(requests.at(-1)?.url).toContain("/api/v1/imports/draft%20%2F%20one/commit");
});

it("does not commit an imported project when unsaved-work confirmation is refused", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const body = init?.method === "PATCH" ? JSON.parse(String(init.body)) : {};
    return new Response(JSON.stringify({ ...draft, ...body, revision: draft.revision + 1 }), {
      status: String(input).endsWith("/imports") ? 201 : 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  const onComplete = vi.fn();
  render(<BookImportWizard beforeCommit={() => false} onComplete={onComplete} onCancel={vi.fn()} />);

  await advanceImportWizardToCommit();
  await userEvent.click(screen.getByRole("button", { name: "创建并导入小说" }));

  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/commit"))).toBe(false);
  expect(onComplete).not.toHaveBeenCalled();
});

const reviewDraft: ImportDraft = {
  ...draft,
  title: "旧城手稿",
  source_kind: "folder",
  files: [
    {
      ...draft.files[0], relative_path: "正文/上卷.md", audit_id: "source-manuscript",
      title: "上卷", size_bytes: 2_048, warning: "检测到重复章节标题",
      content_preview: "",
    },
    {
      ...draft.files[0], relative_path: "任务/下一步.txt", audit_id: "source-task",
      category: "task", title: "下一步", size_bytes: 88, warning: "",
      content_preview: "<img src=x onerror=alert('owned')>\n调查失踪信件",
    },
    {
      ...draft.files[0], relative_path: "杂项/剪贴.txt", audit_id: "source-other",
      category: "other", title: "剪贴", encoding: "gb18030", size_bytes: 24,
      warning: "无法自动分类", content_preview: "旧报纸摘录",
    },
  ],
  chapters: [
    { ...draft.chapters[0], source_document_id: "source-manuscript", draft_chapter_id: "chapter-1", relative_path: "正文/上卷.md", title: "第一章", order_index: 0 },
    { ...draft.chapters[0], source_document_id: "source-manuscript", draft_chapter_id: "chapter-2", relative_path: "正文/上卷.md", title: "第二章", order_index: 1 },
    { ...draft.chapters[0], source_document_id: "source-manuscript", draft_chapter_id: "chapter-3", relative_path: "正文/上卷.md", title: "第三章", order_index: 2 },
  ],
};

it("announces import-limit loading once through an atomic polite status", () => {
  vi.spyOn(api, "importLimits").mockReturnValue(new Promise<ImportLimits>(() => undefined));

  render(<BookImportWizard onComplete={vi.fn()} onCancel={vi.fn()} />);

  const status = screen.getByRole("status", { name: "导入限制状态" });
  expect(status).toHaveAttribute("aria-live", "polite");
  expect(status).toHaveAttribute("aria-atomic", "true");
  expect(status).toHaveTextContent("正在读取实际导入限制");
  expect(status.querySelector("[role='status']")).not.toBeInTheDocument();
});

it("shows the server effective limits on the source step without hard-coded defaults", async () => {
  vi.spyOn(api, "importLimits").mockResolvedValue(effectiveImportLimits);

  render(<BookImportWizard onComplete={vi.fn()} onCancel={vi.fn()} />);

  const limits = await screen.findByRole("region", { name: "当前导入安全限制" });
  expect(limits).toHaveTextContent("最多 12 个文本文件");
  expect(limits).toHaveTextContent(/单文件\s*2.0 MiB/);
  expect(limits).toHaveTextContent(/总展开量\s*24.0 MiB/);
  expect(limits).toHaveTextContent(/ZIP 压缩比\s*8:1/);
  expect(limits).not.toHaveTextContent("1,000");
  expect(screen.queryByRole("status", { name: "导入限制状态" })).not.toBeInTheDocument();
});

it("keeps source selection usable when effective limits cannot be read and does not guess defaults", async () => {
  vi.spyOn(api, "importLimits").mockRejectedValue(new Error("限制服务暂不可用"));
  render(<BookImportWizard onComplete={vi.fn()} onCancel={vi.fn()} />);

  const status = await screen.findByRole("status", { name: "导入限制状态" });
  expect(status).toHaveAttribute("aria-live", "polite");
  expect(status).toHaveAttribute("aria-atomic", "true");
  expect(status).toHaveTextContent(/暂时无法读取服务器的实际导入限制/);
  expect(screen.getAllByRole("status", { name: "导入限制状态" })).toHaveLength(1);
  expect(screen.queryByText(/1,000 个文本文件/)).not.toBeInTheDocument();
  const file = new File(["正文"], "第一章.md", { type: "text/markdown" });
  Object.defineProperty(file, "webkitRelativePath", { value: "旧城/正文/第一章.md" });
  await userEvent.upload(screen.getByLabelText("小说文件夹"), file);
  expect(screen.getByText("已选择 1 个文件")).toBeVisible();
  expect(screen.getByRole("button", { name: "下一步：检查章节" })).toBeEnabled();
});

function returnedDraft(base: ImportDraft, patch: Partial<ImportDraft>, revision: number): ImportDraft {
  return { ...base, ...patch, revision };
}

async function uploadFolderAndOpenReview(created: ImportDraft = reviewDraft, onComplete = vi.fn()) {
  const first = new File(["第一章"], "上卷.md", { type: "text/markdown" });
  const second = new File(["续写"], "下一步.txt", { type: "text/plain" });
  Object.defineProperty(first, "webkitRelativePath", { value: "旧城/正文/上卷.md" });
  Object.defineProperty(second, "webkitRelativePath", { value: "旧城/任务/下一步.txt" });
  const create = vi.spyOn(api, "createImportDraft").mockResolvedValue(structuredClone(created));
  render(<BookImportWizard onComplete={onComplete} onCancel={vi.fn()} />);

  await userEvent.upload(screen.getByLabelText("小说文件夹"), [first, second]);
  await userEvent.click(screen.getByRole("button", { name: "下一步：检查章节" }));
  await screen.findByRole("heading", { name: "检查文件与章节" });
  return { create, first, second, onComplete };
}

it("uploads folder files with parallel relative paths and exposes the ordered progress semantics", async () => {
  const { create } = await uploadFolderAndOpenReview();
  const body = create.mock.calls[0][0];

  expect(body.get("source_kind")).toBe("folder");
  expect(body.getAll("files")).toHaveLength(2);
  expect(body.getAll("paths")).toEqual(["旧城/正文/上卷.md", "旧城/任务/下一步.txt"]);
  const progress = screen.getByRole("list", { name: "导入进度" });
  expect(progress).toHaveTextContent("01选择来源");
  expect(progress).toHaveTextContent("02检查分类与章节");
  expect(progress.querySelector('[aria-current="step"]')).toHaveTextContent("检查分类与章节");
});

it("rejects multiple or non-zip selections locally and keeps upload errors on the source step", async () => {
  const create = vi.spyOn(api, "createImportDraft");
  render(<BookImportWizard onComplete={vi.fn()} onCancel={vi.fn()} />);
  const zipInput = screen.getByLabelText("ZIP 文件");

  fireEvent.change(zipInput, { target: { files: [new File(["a"], "a.zip"), new File(["b"], "b.zip")] } });
  expect(screen.getByRole("alert")).toHaveTextContent("只能选择一个 ZIP 文件");
  fireEvent.change(zipInput, { target: { files: [new File(["x"], "not-a-zip.txt")] } });
  expect(screen.getByRole("alert")).toHaveTextContent("请选择 .zip 文件");
  expect(create).not.toHaveBeenCalled();

  fireEvent.change(zipInput, { target: { files: [new File(["z"], "book.zip")] } });
  create.mockRejectedValueOnce(new Error("上传中断，请重试"));
  await userEvent.click(screen.getByRole("button", { name: "下一步：检查章节" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("上传中断，请重试");
  expect(screen.getByRole("heading", { name: "选择导入来源" })).toBeVisible();
});

it("shows all file metadata and escaped previews, edits categories, and can exclude then restore a file", async () => {
  await uploadFolderAndOpenReview();

  expect(screen.getByText("杂项/剪贴.txt")).toBeVisible();
  expect(screen.getByText("GB18030", { exact: false })).toBeVisible();
  expect(screen.getByText("24 B", { exact: false })).toBeVisible();
  expect(screen.getByText("无法自动分类")).toBeVisible();
  expect(screen.getAllByText("其他资料").length).toBeGreaterThan(0);
  expect(screen.getByText("<img src=x onerror=alert('owned')>", { exact: false })).toBeVisible();
  expect(document.querySelector("img")).toBeNull();

  await userEvent.selectOptions(screen.getByRole("combobox", { name: "杂项/剪贴.txt 分类" }), "world");
  await userEvent.click(screen.getByRole("button", { name: "排除 杂项/剪贴.txt" }));
  expect(screen.getByRole("button", { name: "恢复 杂项/剪贴.txt" })).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "恢复 杂项/剪贴.txt" }));
  expect(screen.getByRole("combobox", { name: "杂项/剪贴.txt 分类" })).toHaveValue("world");
});

it("renames and keyboard-reorders chapters, then PATCHes the full review before advancing", async () => {
  await uploadFolderAndOpenReview();
  const patch = vi.spyOn(api, "patchImportDraft").mockImplementation(async (_id, payload) => (
    returnedDraft(reviewDraft, { files: payload.files, chapters: payload.chapters }, 8)
  ));

  const chapterTitle = screen.getByRole("textbox", { name: "章节名称 第一章" });
  await userEvent.clear(chapterTitle);
  await userEvent.type(chapterTitle, "雨夜旧城");
  await userEvent.click(screen.getByRole("button", { name: "下移 雨夜旧城" }));
  await userEvent.click(screen.getByRole("button", { name: "下一步：确认续写点" }));

  expect(await screen.findByRole("heading", { name: "确认续写点" })).toBeVisible();
  expect(patch).toHaveBeenCalledWith("draft / one", expect.objectContaining({
    revision: 1,
    chapters: [
      expect.objectContaining({ draft_chapter_id: "chapter-2", order_index: 0 }),
      expect.objectContaining({ draft_chapter_id: "chapter-1", title: "雨夜旧城", order_index: 1 }),
      expect.objectContaining({ draft_chapter_id: "chapter-3", order_index: 2 }),
    ],
  }));
});

it("validates continuation order, persists stable chapter ids and explicit confirmation", async () => {
  await uploadFolderAndOpenReview();
  vi.spyOn(api, "patchImportDraft")
    .mockResolvedValueOnce(returnedDraft(reviewDraft, {}, 2))
    .mockImplementationOnce(async (_id, payload) => returnedDraft(reviewDraft, {
      continuation: payload.continuation,
      objective: payload.objective,
    }, 3));
  await userEvent.click(screen.getByRole("button", { name: "下一步：确认续写点" }));

  await userEvent.selectOptions(screen.getByLabelText("已完成到"), "chapter-2");
  await userEvent.selectOptions(screen.getByLabelText("当前未完成章节"), "chapter-1");
  await userEvent.click(screen.getByLabelText("我已确认续写边界"));
  await userEvent.click(screen.getByRole("button", { name: "下一步：创建项目" }));
  expect(screen.getByRole("alert")).toHaveTextContent("当前未完成章节不能早于已完成章节");

  await userEvent.selectOptions(screen.getByLabelText("当前未完成章节"), "chapter-3");
  await userEvent.type(screen.getByLabelText("下一步写作目标"), "从第三章断点继续调查失踪信件");
  await userEvent.click(screen.getByLabelText("我已确认续写边界"));
  await userEvent.click(screen.getByRole("button", { name: "下一步：创建项目" }));
  expect(await screen.findByRole("heading", { name: "创建导入项目" })).toBeVisible();
  expect(api.patchImportDraft).toHaveBeenLastCalledWith("draft / one", expect.objectContaining({
    continuation: expect.objectContaining({
      confirmed: true,
      completed_through_node_id: "chapter-2",
      current_chapter_id: "chapter-3",
    }),
  }));
});

it("saves the title with the returned revision, shows a commit summary, and prevents double commit", async () => {
  const onComplete = vi.fn();
  await uploadFolderAndOpenReview(reviewDraft, onComplete);
  const patch = vi.spyOn(api, "patchImportDraft")
    .mockResolvedValueOnce(returnedDraft(reviewDraft, {}, 2))
    .mockResolvedValueOnce(returnedDraft(reviewDraft, {
      continuation: { ...reviewDraft.continuation, confirmed: true, current_chapter_id: "chapter-3", objective: "继续调查" },
      objective: "继续调查",
    }, 3))
    .mockResolvedValueOnce(returnedDraft(reviewDraft, {
      title: "旧城新稿",
      continuation: { ...reviewDraft.continuation, confirmed: true, current_chapter_id: "chapter-3", objective: "继续调查" },
    }, 9));
  let resolveCommit!: (value: ImportCommitResponse) => void;
  const commitPromise = new Promise<ImportCommitResponse>((resolve) => { resolveCommit = resolve; });
  const commit = vi.spyOn(api, "commitImportDraft").mockReturnValue(commitPromise);
  // The helper rendered the component; replace its callback by starting this journey with the same public behavior.
  vi.mocked(api.createImportDraft).mockClear();
  const wizard = screen.getByLabelText("导入进度").closest(".book-import-wizard");
  expect(wizard).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "下一步：确认续写点" }));
  await userEvent.click(screen.getByLabelText("我已确认续写边界"));
  await userEvent.click(screen.getByRole("button", { name: "下一步：创建项目" }));
  expect(screen.getByText("3 个章节", { exact: false })).toBeVisible();
  expect(screen.getByText("任务说明").closest("div")).toHaveTextContent("任务说明1");
  expect(screen.getByText("继续调查", { exact: false })).toBeVisible();

  const title = screen.getByRole("textbox", { name: "小说名称" });
  await userEvent.clear(title);
  await userEvent.type(title, "旧城新稿");
  const submit = screen.getByRole("button", { name: "创建并导入小说" });
  await userEvent.dblClick(submit);
  await waitFor(() => expect(commit).toHaveBeenCalledOnce());
  expect(patch).toHaveBeenLastCalledWith("draft / one", { revision: 3, title: "旧城新稿" });
  expect(commit).toHaveBeenCalledWith("draft / one", 9);
  resolveCommit(commitResponse);
  await waitFor(() => expect(onComplete).toHaveBeenCalledWith(commitResponse));
});

it("uses an in-app discard confirmation and stays open when DELETE fails", async () => {
  await uploadFolderAndOpenReview();
  const discard = vi.spyOn(api, "discardImportDraft").mockRejectedValue(new Error("清理失败"));
  const confirmSpy = vi.spyOn(window, "confirm");

  await userEvent.click(screen.getByRole("button", { name: "取消导入" }));
  expect(screen.getByText("放弃这次导入？")).toBeVisible();
  expect(screen.getByRole("button", { name: "继续编辑" })).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "放弃导入" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("清理失败");
  expect(discard).toHaveBeenCalledWith("draft / one");
  expect(confirmSpy).not.toHaveBeenCalled();
  expect(screen.getByRole("alertdialog", { name: "放弃这次导入？" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "检查文件与章节" })).not.toBeInTheDocument();
});
