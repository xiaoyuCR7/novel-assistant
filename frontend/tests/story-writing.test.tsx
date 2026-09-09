import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode, useState } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProjectLauncher } from "../src/features/projects/ProjectLauncher";
import { EntityStudio } from "../src/features/story/EntityStudio";
import { StoryMap } from "../src/features/story/StoryMap";
import { ChapterWorkspace } from "../src/features/writing/ChapterWorkspace";
import { VersionPanel } from "../src/features/writing/VersionPanel";
import type { ChapterDocument } from "../src/lib/types";

const versionSummary = (id: string, summary: string, source = "manual") => ({
  id,
  chapter_id: "c1",
  created_at: "2026-08-30",
  source,
  summary,
  word_count: 42,
  parent_version_id: null,
  restored_from_version_id: null,
  generation_job_id: source === "ai" ? "job-1" : null,
});

describe("story and writing workspaces", () => {
  it.each([
    "secret A, secret B",
    "secret A，secret B",
    "secret A\nsecret B",
    "  secret A , \n， secret B  \n",
    " \n，, \n ",
  ])("treats saved forbidden revelations semantically and preserves raw input: %j", async (raw) => {
    const initial: ChapterDocument = { content: "draft", contract: {}, current_version_id: null, revision: 1 };
    const onDirtyChange = vi.fn();
    const onSave = vi.fn();
    function Roundtrip() {
      const [document, setDocument] = useState(initial);
      return <ChapterWorkspace title="Chapter" document={document} saving={false}
        onDirtyChange={onDirtyChange}
        onSave={async (submitted) => {
          onSave(submitted);
          const saved = { ...initial, ...submitted, revision: 2 };
          setDocument(saved);
          return saved;
        }} />;
    }
    render(<Roundtrip />);
    fireEvent.change(screen.getByLabelText("禁止提前揭示"), { target: { value: raw } });
    await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      revision: initial.revision,
      contract: { purpose: "", forbidden_revelations: raw.includes("secret") ? ["secret A", "secret B"] : [] },
    }));
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    expect(screen.getByLabelText("禁止提前揭示")).toHaveValue(raw);
  });

  it.each(["content", "purpose", "forbidden", "all"])("preserves late %s edits during a normalized save roundtrip", async (field) => {
    const initial: ChapterDocument = { content: "draft", contract: {}, current_version_id: null, revision: 1 };
    const onDirtyChange = vi.fn();
    const onSave = vi.fn();
    let releaseSave!: () => void;
    function Roundtrip() {
      const [document, setDocument] = useState(initial);
      return <ChapterWorkspace title="Chapter" document={document} saving={false}
        onDirtyChange={onDirtyChange}
        onSave={async (submitted) => {
          onSave(submitted);
          const saved = { ...initial, ...submitted, revision: document.revision! + 1 };
          await new Promise<void>((resolve) => { releaseSave = resolve; });
          setDocument(saved);
          return saved;
        }} />;
    }
    render(<Roundtrip />);
    fireEvent.change(screen.getByLabelText("禁止提前揭示"), { target: { value: "secret A, secret B" } });
    await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
    const content = field === "content" || field === "all" ? "late content" : "draft";
    const purpose = field === "purpose" || field === "all" ? "late purpose" : "";
    const forbidden = field === "forbidden" || field === "all" ? "secret A, secret B， new secret" : "secret A, secret B";
    fireEvent.change(screen.getByLabelText("章节正文"), { target: { value: content } });
    fireEvent.change(screen.getByLabelText("本章目的"), { target: { value: purpose } });
    fireEvent.change(screen.getByLabelText("禁止提前揭示"), { target: { value: forbidden } });
    await act(async () => releaseSave());

    expect(screen.getByLabelText("章节正文")).toHaveValue(content);
    expect(screen.getByLabelText("本章目的")).toHaveValue(purpose);
    expect(screen.getByLabelText("禁止提前揭示")).toHaveValue(forbidden);
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);

    await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
    expect(onSave).toHaveBeenLastCalledWith({
      content, revision: 2,
      contract: { purpose, forbidden_revelations: forbidden.includes("new secret") ? ["secret A", "secret B", "new secret"] : ["secret A", "secret B"] },
    });
    await act(async () => releaseSave());
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
    expect(screen.getByLabelText("禁止提前揭示")).toHaveValue(forbidden);
  });

  it("retains the raw draft fingerprint for format-only late edits during replacement", () => {
    const initial: ChapterDocument = {
      content: "original", contract: { forbidden_revelations: ["secret A", "secret B"] },
      current_version_id: "v1", revision: 1,
    };
    const onDraftChange = vi.fn();
    const props = { title: "Chapter", document: initial, saving: false, onSave: vi.fn(), onDraftChange };
    const view = render(<ChapterWorkspace {...props} />);
    const requestedDraft = onDraftChange.mock.lastCall![0];
    fireEvent.change(screen.getByLabelText("禁止提前揭示"), { target: { value: "secret A, secret B" } });
    expect(onDraftChange).toHaveBeenLastCalledWith(JSON.stringify(["original", "", "secret A, secret B"]));
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();

    const restored = { ...initial, content: "restored", current_version_id: "v2", revision: 2 };
    view.rerender(<ChapterWorkspace {...props} document={restored} replacement={{ document: restored, draft: requestedDraft }} />);

    expect(screen.getByLabelText("章节正文")).toHaveValue("original");
    expect(screen.getByLabelText("禁止提前揭示")).toHaveValue("secret A, secret B");
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
  });

  it("keeps the saved revision when the parent document refresh is delayed or fails", async () => {
    const initial: ChapterDocument = { content: "old content", contract: {}, current_version_id: null, revision: 1 };
    const saved: ChapterDocument = {
      ...initial, content: "saved content", revision: 2,
      contract: { purpose: "saved purpose", forbidden_revelations: ["secret A", "secret B"] },
    };
    const onSave = vi.fn().mockResolvedValue(saved);
    const props = { title: "Chapter", document: initial, saving: false, onSave };
    const view = render(<ChapterWorkspace {...props} />);
    fireEvent.change(screen.getByLabelText("章节正文"), { target: { value: saved.content } });
    fireEvent.change(screen.getByLabelText("本章目的"), { target: { value: "saved purpose" } });
    fireEvent.change(screen.getByLabelText("禁止提前揭示"), { target: { value: "secret A, secret B" } });
    await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));

    expect(screen.getByLabelText("章节正文")).toHaveValue(saved.content);
    expect(screen.getByLabelText("本章目的")).toHaveValue("saved purpose");
    expect(screen.getByLabelText("禁止提前揭示")).toHaveValue("secret A, secret B");
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
    expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ content: saved.content, revision: saved.revision }));

    const newer = { ...saved, content: "newer server content", revision: 3 };
    view.rerender(<ChapterWorkspace {...props} document={newer} />);
    expect(screen.getByLabelText("章节正文")).toHaveValue(newer.content);
    view.rerender(<ChapterWorkspace {...props} />);
    expect(screen.getByLabelText("章节正文")).toHaveValue(newer.content);
    fireEvent.change(screen.getByLabelText("章节正文"), { target: { value: "late local draft" } });
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    view.rerender(<ChapterWorkspace {...props} document={{ ...newer, content: "another update", revision: 4 }} />);
    expect(screen.getByLabelText("章节正文")).toHaveValue("late local draft");
    expect(screen.getByRole("alert")).toHaveTextContent("其他窗口已更新本章");
  });

  it("locks structural inputs while a previous save is pending", async () => {
    render(
      <StoryMap
        nodes={[]}
        plots={[]}
        selectedNodeId={null}
        onSelectNode={vi.fn()}
        onCreateNode={() => new Promise(() => {})}
      />,
    );
    await userEvent.type(screen.getByLabelText("节点标题"), "第一章");
    await userEvent.click(screen.getByRole("button", { name: "加入故事地图" }));
    expect(screen.getByLabelText("节点标题")).toBeDisabled();
    expect(screen.getByRole("button", { name: "加入故事地图" })).toBeDisabled();
  });
  it("creates a project from a writer-facing brief", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockResolvedValue(undefined);
    render(<ProjectLauncher onCreate={onCreate} busy={false} />);

    await user.type(screen.getByLabelText("小说名称"), "雾城来信");
    await user.type(screen.getByLabelText("核心前提"), "失忆邮差替死者送信");
    await user.type(screen.getByLabelText("类型"), "奇幻悬疑");
    await user.click(screen.getByRole("button", { name: "新建空白小说" }));

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "雾城来信",
        premise: "失忆邮差替死者送信",
        genre: "奇幻悬疑",
      }),
    );
  });

  it("adds a structural node and selects an existing chapter", async () => {
    const user = userEvent.setup();
    const onCreateNode = vi.fn().mockResolvedValue(undefined);
    const onSelectNode = vi.fn();
    const nodes = [
      {
        id: "v1",
        kind: "volume" as const,
        title: "第一卷",
        status: "active",
        order_index: 1,
      },
      {
        id: "c1",
        kind: "chapter" as const,
        title: "雾中投递",
        status: "planned",
        order_index: 1,
        parent_id: "v1",
      },
    ];
    render(
      <StoryMap
        nodes={nodes}
        plots={[]}
        selectedNodeId="c1"
        onSelectNode={onSelectNode}
        onCreateNode={onCreateNode}
      />,
    );

    await user.click(screen.getByRole("button", { name: "雾中投递" }));
    expect(onSelectNode).toHaveBeenCalledWith("c1");

    await user.selectOptions(screen.getByLabelText("节点类型"), "scene");
    await user.type(screen.getByLabelText("节点标题"), "钟楼下的交接");
    await user.click(screen.getByRole("button", { name: "加入故事地图" }));
    expect(onCreateNode).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "scene",
        title: "钟楼下的交接",
        parent_id: "c1",
      }),
    );
  });

  it("captures a character profile without exposing JSON to the writer", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockResolvedValue(undefined);
    render(<EntityStudio entities={[]} onCreate={onCreate} />);

    await user.type(screen.getByLabelText("姓名或名称"), "林渡");
    await user.type(screen.getByLabelText("人物简介"), "二十四岁的雾城邮差");
    await user.type(screen.getByLabelText("声音特征"), "寡言，回避直接承诺");
    await user.click(screen.getByRole("button", { name: "保存人物" }));

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "character",
        name: "林渡",
        profile: expect.objectContaining({ voice: "寡言，回避直接承诺" }),
      }),
    );
  });

  it("creates locations, organizations, and items as world entities", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockResolvedValue(undefined);
    render(<EntityStudio entities={[]} onCreate={onCreate} />);

    await user.selectOptions(screen.getByLabelText("实体类型"), "location");
    await user.type(screen.getByLabelText("姓名或名称"), "第七码头");
    await user.type(
      screen.getByLabelText("人物简介"),
      "只在退潮后出现的旧码头",
    );
    await user.click(screen.getByRole("button", { name: "保存地点" }));

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "location",
        name: "第七码头",
      }),
    );
  });

  it("saves manuscript and chapter contract together", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <ChapterWorkspace
        title="雾中投递"
        document={{ content: "旧稿", contract: {}, current_version_id: null }}
        saving={false}
        onSave={onSave}
      />,
    );

    const editor = screen.getByLabelText("章节正文");
    await user.clear(editor);
    await user.type(editor, "雨落在无人签收的信封上。");
    await user.type(screen.getByLabelText("本章目的"), "让林渡接下无主邮袋");
    await user.type(screen.getByLabelText("禁止提前揭示"), "寄信人的身份");
    await user.click(screen.getByRole("button", { name: "保存工作副本" }));

    expect(onSave).toHaveBeenCalledWith({
      content: "雨落在无人签收的信封上。",
      contract: expect.objectContaining({
        purpose: "让林渡接下无主邮袋",
        forbidden_revelations: ["寄信人的身份"],
      }),
    });
  });

  it("shows version lineage, diff, and an explicit non-destructive restore action", async () => {
    const user = userEvent.setup();
    const onCompare = vi.fn().mockResolvedValue("-雨\n+雪");
    const onRestore = vi.fn().mockResolvedValue(undefined);
    render(
      <VersionPanel
        versions={[
          versionSummary("v1", "初稿"),
          versionSummary("v2", "天气改写"),
        ]}
        onCompare={onCompare}
        onRestore={onRestore}
      />,
    );

    await user.selectOptions(screen.getByLabelText("基准版本"), "v1");
    await user.selectOptions(screen.getByLabelText("比较版本"), "v2");
    await user.click(screen.getByRole("button", { name: "比较版本" }));
    expect(
      await screen.findByText("-雨", { exact: false }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "从初稿创建恢复版本" }),
    );
    expect(onRestore).toHaveBeenCalledWith("v1");
    expect(
      screen.getByText("恢复会创建新版本，不改写历史。", { exact: false }),
    ).toBeInTheDocument();
  });

  it("loads more version summaries once when another page exists", async () => {
    const onLoadMore = vi.fn();
    render(
      <VersionPanel
        versions={[versionSummary("v1", "最新版本")]}
        hasMore
        loadingMore={false}
        onLoadMore={onLoadMore}
        onCompare={vi.fn()}
        onRestore={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "加载更多" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("disables the version loader and announces progress while loading", () => {
    render(
      <VersionPanel
        versions={[]}
        hasMore
        loadingMore
        onLoadMore={vi.fn()}
        onCompare={vi.fn()}
        onRestore={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "加载中…" })).toBeDisabled();
  });

  it("hides the version loader when there are no more pages", () => {
    render(
      <VersionPanel
        versions={[]}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onCompare={vi.fn()}
        onRestore={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "加载更多" })).not.toBeInTheDocument();
  });

  it("announces the initial version load", () => {
    render(
      <VersionPanel
        versions={[]}
        initialLoading
        onCompare={vi.fn()}
        onRestore={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在加载历史版本…");
  });

  it("clears a displayed diff when either comparison selection changes", async () => {
    render(
      <VersionPanel
        versions={[
          versionSummary("v1", "one"),
          versionSummary("v2", "two"),
          versionSummary("v3", "three"),
        ]}
        onCompare={vi.fn().mockResolvedValue("current diff")}
        onRestore={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "比较版本" }));
    expect(await screen.findByText("current diff")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("比较版本"), "v3");
    expect(screen.queryByText("current diff")).not.toBeInTheDocument();
  });

  it("disables duplicate comparison while the selected pair is pending", async () => {
    let resolve!: (value: string) => void;
    const onCompare = vi.fn(() => new Promise<string>((done) => { resolve = done; }));
    render(
      <VersionPanel
        versions={[
          versionSummary("v1", "one"),
          versionSummary("v2", "two"),
        ]}
        onCompare={onCompare}
        onRestore={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "比较版本" }));
    expect(screen.getByRole("button", { name: "比较中…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "从one创建恢复版本" })).toBeDisabled();
    expect(onCompare).toHaveBeenCalledTimes(1);
    await act(async () => resolve("done"));
    expect(screen.getByRole("button", { name: "比较版本" })).toBeEnabled();
  });

  it("ignores an older comparison that resolves after the current pair", async () => {
    let resolveFirst!: (value: string) => void;
    let resolveSecond!: (value: string) => void;
    const onCompare = vi.fn()
      .mockImplementationOnce(() => new Promise<string>((done) => { resolveFirst = done; }))
      .mockImplementationOnce(() => new Promise<string>((done) => { resolveSecond = done; }));
    render(
      <VersionPanel
        versions={[
          versionSummary("v1", "one"),
          versionSummary("v2", "two"),
          versionSummary("v3", "three"),
        ]}
        onCompare={onCompare}
        onRestore={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "比较版本" }));
    await userEvent.selectOptions(screen.getByLabelText("比较版本"), "v3");
    await userEvent.click(screen.getByRole("button", { name: "比较版本" }));
    await act(async () => resolveSecond("new comparison"));
    expect(screen.getByText("new comparison")).toBeInTheDocument();
    await act(async () => resolveFirst("stale comparison"));
    expect(screen.getByText("new comparison")).toBeInTheDocument();
    expect(screen.queryByText("stale comparison")).not.toBeInTheDocument();
  });

  it("completes a comparison after the StrictMode effect replay", async () => {
    let resolve!: (value: string) => void;
    render(
      <StrictMode>
        <VersionPanel
          versions={[versionSummary("v1", "初稿"), versionSummary("v2", "改稿")]}
          onCompare={() => new Promise<string>((done) => { resolve = done; })}
          onRestore={vi.fn()}
        />
      </StrictMode>,
    );

    await userEvent.click(screen.getByRole("button", { name: "比较版本" }));
    expect(screen.getByRole("button", { name: "比较中…" })).toBeDisabled();
    await act(async () => resolve("StrictMode comparison"));

    expect(screen.getByText("StrictMode comparison")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "比较版本" })).toBeEnabled();
  });

  it("invalidates a pending comparison when refreshed versions change the effective pair", async () => {
    let resolve!: (value: string) => void;
    const onCompare = vi.fn(() => new Promise<string>((done) => { resolve = done; }));
    const onRestore = vi.fn();
    const view = render(
      <VersionPanel
        versions={[versionSummary("v1", "初稿"), versionSummary("v2", "改稿")]}
        onCompare={onCompare}
        onRestore={onRestore}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "比较版本" }));

    view.rerender(
      <VersionPanel
        versions={[
          versionSummary("v0", "刷新后最新版本", "ai"),
          versionSummary("v1", "初稿"),
          versionSummary("v2", "改稿"),
        ]}
        onCompare={onCompare}
        onRestore={onRestore}
      />,
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "比较版本" })).toBeEnabled());
    expect(screen.getByLabelText("基准版本")).toHaveValue("v0");
    expect(screen.getByLabelText("比较版本")).toHaveValue("v1");

    await act(async () => resolve("stale after refresh"));
    expect(screen.queryByText("stale after refresh")).not.toBeInTheDocument();
  });

  it("uses the backend AI summary as the visible version label", () => {
    render(
      <VersionPanel
        versions={[versionSummary("v-ai", "AI 摘要：暴雨夜改写", "ai")]}
        onCompare={vi.fn()}
        onRestore={vi.fn()}
      />,
    );

    expect(screen.getByText("AI 摘要：暴雨夜改写", { selector: "strong" })).toBeInTheDocument();
  });
});
