import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/app/App";
import { AppProviders } from "../src/app/providers";
import { emptyLibraryPage, emptyWorkspaceView } from './library-fixtures';

function json(data: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(data), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("integrated studio", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("loads a project and exposes every core creation workspace", async () => {
    const project = {
      id: "p1",
      title: "雾城来信",
      premise: "死者寄信",
      genre: "奇幻悬疑",
      target_words: 200000,
      daily_goal: 1500,
      status: "active",
    };
    const chapter = {
      id: "c1",
      kind: "chapter",
      title: "雾中投递",
      status: "drafting",
      order_index: 1,
      parent_id: null,
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      const page = emptyLibraryPage(url); if (page) return json(page);
      const viewData = emptyWorkspaceView(url, [chapter]); if (viewData) return json(viewData);
      if (url.includes("/ai/jobs/page?")) return json({ items: [], next_cursor: null });
      if (url.endsWith("/settings/model"))
        return json({
          mode: "demo",
          base_url: "",
          model: "",
          has_api_key: false,
          external_consent: false,
        });
      if (url.endsWith("/api/v1/health")) return json({ ai_provider: "demo" });
      if (url.endsWith("/rag/health"))
        return json({ vectors: "disabled", documents: 0 });
      if (url.endsWith("/api/v1/projects")) return json([project]);
      if (url.endsWith('/workspace/navigation'))
        return json({
          project,
          nodes: [chapter],
        });
      if (url.endsWith("/api/v1/projects/p1/chapters/c1"))
        return json({
          chapter_id: "c1",
          project_id: "p1",
          content: "雨落在信封上。",
          contract: { purpose: "收到遗书" },
          current_version_id: null,
        });
      if (url.endsWith("/api/v1/projects/p1/chapters/c1/versions"))
        return json([]);
      if (url.endsWith("/api/v1/projects/p1/progress"))
        return json({
          current_words: 7,
          target_words: 200000,
          completion_ratio: 0.000035,
          chapter_count: 1,
          completed_chapters: 0,
          daily_goal: 1500,
        });
      if (url.endsWith("/api/v1/projects/p1/assets")) return json([]);
      if (url.endsWith("/api/v1/projects/p1/preferences")) return json([]);
      if (url.endsWith("/api/v1/projects/p1/conflicts")) return json([]);
      if (url.endsWith('/styles/active-context')) return json({ rules: [] });
      if (url.endsWith("/summary")) return json(null);
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(
      <AppProviders>
        <App />
      </AppProviders>,
    );

    expect(await screen.findByLabelText("给 AI 的消息")).toBeInTheDocument();
    expect(screen.queryByLabelText("章节正文")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看正文" }));
    expect(await screen.findByText("雨落在信封上。")).toBeInTheDocument();
    expect(screen.getByRole("banner")).toHaveTextContent("雾城来信");
    await user.click(screen.getByRole("button", { name: "资料库" }));
    expect(screen.getByRole("link", { name: "导出备份" })).toHaveAttribute(
      "href",
      "/api/v1/projects/p1/export",
    );

    for (const [tab, heading] of [
      ["ideas", "先捕捉火花，再决定它属于哪里"],
      ["story", "结构与承诺"],
      ["entities", "让人物拥有自己的重力"],
      ["knowledge", "把确定的事实与时间固定下来"],
      ["conflicts", "问题不是红灯，是分岔路"],
      ["style", "偏好必须经过你的确认"],
      ["assets", "给人物与地点一张可回看的脸"],
    ]) {
      await user.selectOptions(screen.getByLabelText("资料工作区"), tab);
      expect(
        await screen.findByRole("heading", { name: heading }),
      ).toBeInTheDocument();
    }
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/projects",
      expect.any(Object),
    );
  });

  it("activates a newly created project before the project-list refresh returns", async () => {
    const oldProject = {
      id: "old-project", title: "旧项目", premise: "旧前提", genre: "悬疑",
      target_words: 200000, daily_goal: 1500, status: "active",
    };
    const newProject = {
      id: "new-project", title: "雾城来信", premise: "失忆邮差送信", genre: "奇幻悬疑",
      target_words: 200000, daily_goal: 1500, status: "active",
    };
    const oldChapter = {
      id: "old-chapter", kind: "chapter", title: "旧章节", status: "drafting",
      order_index: 0, parent_id: null,
    };
    let projectReads = 0;
    const delayedProjectRefresh = new Promise<Response>(() => undefined);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/projects") && init?.method === "POST") return json(newProject, 201);
      if (url.endsWith("/api/v1/projects")) {
        projectReads += 1;
        return projectReads === 1 ? json([oldProject]) : delayedProjectRefresh;
      }
      const activeProject = url.includes("/new-project/") ? newProject : oldProject;
      const page = emptyLibraryPage(url); if (page) return json(page);
      const viewData = emptyWorkspaceView(url, []); if (viewData) return json(viewData);
      if (url.endsWith("/workspace/navigation")) {
        return json({ project: activeProject, nodes: activeProject.id === oldProject.id ? [oldChapter] : [] });
      }
      if (url.includes("/ai/jobs/page?")) return json({ items: [], next_cursor: null });
      if (url.includes("/versions/page?")) return json({ items: [], next_cursor: null });
      if (url.endsWith(`/chapters/${oldChapter.id}`)) return json({
        chapter_id: oldChapter.id, project_id: oldProject.id, content: "旧正文", contract: {},
        current_version_id: null, revision: 1,
      });
      if (url.endsWith("/summary")) return json(null);
      if (url.endsWith("/settings/model")) return json({
        mode: "demo", base_url: "", model: "", has_api_key: false, external_consent: false,
      });
      if (url.endsWith("/progress")) return json({
        current_words: 0, target_words: 200000, completion_ratio: 0,
        chapter_count: 0, completed_chapters: 0, daily_goal: 1500,
      });
      if (url.endsWith("/conflicts")) return json([]);
      if (url.endsWith("/rag/health")) return json({ vectors: "disabled", documents: 0 });
      if (url.endsWith("/imports/latest/analysis")) return json({}, 404);
      return new Promise<Response>(() => undefined);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();

    render(<AppProviders><App /></AppProviders>);

    expect(await screen.findByLabelText("当前小说项目")).toHaveValue(oldProject.id);
    await user.click(screen.getByRole("button", { name: "查看正文" }));
    await user.type(await screen.findByLabelText("章节正文"), "未保存");
    await user.click(screen.getByRole("button", { name: "创建新小说" }));
    await user.type(screen.getByLabelText("小说名称"), newProject.title);
    await user.type(screen.getByLabelText("核心前提"), newProject.premise);
    await user.type(screen.getByLabelText("类型"), newProject.genre);
    await user.click(screen.getByRole("button", { name: "新建空白小说" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "创建独立小说项目" })).not.toBeInTheDocument());
    expect(screen.getByLabelText("当前小说项目")).toHaveValue(newProject.id);
    expect(projectReads).toBe(2);

    const leave = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(leave);
    expect(leave.defaultPrevented).toBe(false);
    await user.click(screen.getByRole("button", { name: "资料库" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(await screen.findByLabelText("资料工作区")).toBeInTheDocument();
  });
});
