import { render, screen, act, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { VersionPanel } from "../src/features/writing/VersionPanel";
import { ChapterWorkspace } from "../src/features/writing/ChapterWorkspace";
import { StoryMap } from "../src/features/story/StoryMap";
import { App } from "../src/app/App";
import { emptyLibraryPage } from './library-fixtures';
import { AppProviders } from "../src/app/providers";
import { ChatWorkspace } from "../src/features/ai/ChatWorkspace";
import { ModelSettings } from "../src/features/settings/ModelSettings";
import { api, ApiError } from "../src/lib/api";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); });

it("initializes comparison when asynchronous versions arrive", () => {
  const props = { versions: [], onCompare: vi.fn(), onRestore: vi.fn() };
  const view = render(<VersionPanel {...props} />);
  view.rerender(<VersionPanel {...props} versions={[{
    id: "v1", chapter_id: "c1", created_at: "", source: "manual", summary: "one", word_count: 1,
    parent_version_id: null, restored_from_version_id: null, generation_job_id: null,
  }]} />);
  expect(screen.getByRole("button", { name: "比较版本" })).toBeEnabled();
});

it("keeps legacy malformed contract text editable without crashing", () => {
  render(<ChapterWorkspace title="chapter" document={{ content: "text", contract: { forbidden_revelations: "legacy value" }, current_version_id: null }} saving={false} onSave={vi.fn()} />);
  expect(screen.getByLabelText("禁止提前揭示")).toHaveValue("legacy value");
});

it("never passes the project-chat UI marker as a story parent", async () => {
  const create = vi.fn().mockResolvedValue(undefined);
  render(<StoryMap nodes={[]} plots={[]} selectedNodeId="project-chat" onSelectNode={vi.fn()} onCreateNode={create} />);
  await userEvent.type(screen.getByLabelText("节点标题"), "new chapter");
  await userEvent.click(screen.getByRole("button", { name: "加入故事地图" }));
  expect(create).toHaveBeenCalledWith(expect.objectContaining({ parent_id: null }));
});

it("protects new typing after an earlier save resolves", async () => {
  const project = { id: "audit-p", title: "Audit novel", premise: "", genre: "", target_words: 1000, daily_goal: 100, status: "active" };
  const chapter = { id: "audit-c", kind: "chapter", title: "Chapter", status: "drafting", order_index: 1, parent_id: null };
  let document = { chapter_id: "audit-c", project_id: "audit-p", content: "original", contract: {}, current_version_id: null, revision: 1 };
  let release: (() => void) | undefined;
  const submitted: Array<{ revision: number }> = [];
  const response = (value: unknown) => Promise.resolve(new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input);
    if (url.includes('/ai/jobs/page?')) return response({ items: [], next_cursor: null });
    if (url.includes('/versions/page?')) return response({ items: [], next_cursor: null });
    if (url.endsWith("/projects")) return response([project, { ...project, id: "another-project", title: "Other novel" }]);
    if (url.endsWith('/workspace/navigation')) return response({ project, nodes: [chapter] });
    const page = emptyLibraryPage(url); if (page) return response(page);
    if (url.endsWith("/chapters/audit-c")) {
      if (options?.method === "PUT") return new Promise<Response>(resolve => { submitted.push(JSON.parse(String(options.body))); release = () => { document = { ...document, ...JSON.parse(String(options.body)), revision: document.revision + 1 }; resolve(new Response(JSON.stringify(document), { status: 200 })); }; });
      return response(document);
    }
    if (url.endsWith("/settings/model")) return response({ mode: "demo", model: "", base_url: "", has_api_key: false, external_consent: false });
    if (url.endsWith("/rag/health")) return response({ vectors: "disabled", documents: 0 });
    if (url.endsWith("/progress")) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 1, completed_chapters: 0, daily_goal: 100 });
    if (url.endsWith("/summary")) return response(null);
    return response([]);
  }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<AppProviders><App /></AppProviders>);
  await screen.findByLabelText("给 AI 的消息");
  await userEvent.click(screen.getByRole("button", { name: "查看正文" }));
  const editor = await screen.findByLabelText("章节正文");
  await userEvent.type(editor, " saved");
  await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
  await waitFor(() => expect(release).toBeTypeOf("function"));
  await userEvent.selectOptions(screen.getByLabelText("当前小说项目"), "another-project");
  expect(confirm).not.toHaveBeenCalled();
  expect(screen.getByLabelText("当前小说项目")).toHaveValue(project.id);
  await userEvent.type(editor, " UNSAVED");
  await act(async () => release?.());
  await waitFor(() => expect(screen.getByRole("button", { name: "保存工作副本" })).toBeEnabled());
  expect(document.content).toBe("original saved");
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  expect(confirm).toHaveBeenCalled();
  expect(editor).toHaveValue("original saved UNSAVED");
  await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
  expect(submitted[1].revision).toBe(2);
  await act(async () => release?.());
});

it("does not send until the provider state is known", () => {
  render(<ChatWorkspace chapterTitle="test" hasChapter={false} jobs={[]} running={false}
    disabledReason="模型状态尚未确认" onSend={vi.fn()} onAccept={vi.fn()} onOpenManuscript={vi.fn()} />);
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
});

it("requires confirmation to repair corrupt model settings", async () => {
  vi.spyOn(api, "modelSettings").mockRejectedValue(new ApiError(503, "corrupt", undefined, "MODEL_CONFIG_UNAVAILABLE"));
  const save = vi.spyOn(api, "saveModelSettings").mockResolvedValue({ mode: "demo", base_url: "", model: "", has_api_key: false, external_consent: false });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<AppProviders><ModelSettings /></AppProviders>);
  const reset = await screen.findByRole("button", { name: "备份并重置配置" });
  await userEvent.click(reset);
  expect(save).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  await userEvent.click(reset);
  expect(save).toHaveBeenCalledWith({ mode: "demo", clear_api_key: true, repair_config: true });
  expect(await screen.findByLabelText("运行模式")).toHaveValue("demo");
});

it("retains unsent messages per project and chapter, including after remount", async () => {
  const props = { chapterTitle: "test", hasChapter: true, jobs: [], running: false, onSend: vi.fn(), onAccept: vi.fn(), onOpenManuscript: vi.fn() };
  const first = render(<ChatWorkspace {...props} draftKey="p1:c1" />);
  await userEvent.type(screen.getByLabelText("给 AI 的消息"), "private draft");
  first.unmount();
  const second = render(<ChatWorkspace {...props} draftKey="p2:c1" />);
  expect(screen.getByLabelText("给 AI 的消息")).toHaveValue("");
  second.unmount();
  render(<ChatWorkspace {...props} draftKey="p1:c1" />);
  expect(screen.getByLabelText("给 AI 的消息")).toHaveValue("private draft");
});
