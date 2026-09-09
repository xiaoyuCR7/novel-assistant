import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/app/App";
import { AppProviders } from "../src/app/providers";
import { emptyLibraryPage } from "./library-fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

const response = (value: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );

const failedResponse = () =>
  Promise.resolve(
    new Response(JSON.stringify({ detail: { message: "sensitive backend detail" } }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    }),
  );

const versionItem = (id: string, chapterId: string, summary: string, createdAt = "") => ({
  id,
  chapter_id: chapterId,
  created_at: createdAt,
  source: "manual",
  summary,
  word_count: 42,
  parent_version_id: null,
  restored_from_version_id: null,
  generation_job_id: null,
});

function stubVersionStudio(
  versionPage: (url: string) => Promise<Response>,
) {
  const project = {
    id: "version-error-p", title: "Version novel", premise: "", genre: "",
    target_words: 1000, daily_goal: 100, status: "active",
  };
  const chapter = {
    id: "version-error-c", kind: "chapter", title: "Version chapter",
    status: "drafting", order_index: 1, parent_id: null,
  };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/projects")) return response([project]);
    if (url.endsWith("/workspace/navigation")) return response({ project, nodes: [chapter] });
    if (url.endsWith("/chapters/version-error-c")) {
      return response({ content: "saved draft", contract: {}, current_version_id: "v-new", revision: 1 });
    }
    if (url.includes("/chapters/version-error-c/versions/page?")) return versionPage(url);
    if (url.includes("/ai/jobs/page?")) return response({ items: [], next_cursor: null });
    if (url.endsWith("/summary")) return response(null);
    if (url.endsWith("/settings/model")) {
      return response({ mode: "demo", model: "", base_url: "", has_api_key: false, external_consent: false });
    }
    if (url.endsWith("/rag/health")) return response({ vectors: "disabled", documents: 0 });
    if (url.endsWith("/progress")) {
      return response({ current_words: 0, target_words: 1000, completion_ratio: 0,
        chapter_count: 1, completed_chapters: 0, daily_goal: 100 });
    }
    const page = emptyLibraryPage(url);
    if (page) return response(page);
    return response([]);
  }));
}

async function openVersionPanel() {
  render(<AppProviders><App /></AppProviders>);
  await screen.findByLabelText("给 AI 的消息");
  await userEvent.click(screen.getByRole("button", { name: "查看正文" }));
  await userEvent.click(await screen.findByText("历史版本"));
}

describe("chapter version pagination", () => {
  it("uses only the summary page endpoint and appends an encoded next page", async () => {
    const project = {
      id: "version-p",
      title: "Version novel",
      premise: "",
      genre: "",
      target_words: 1000,
      daily_goal: 100,
      status: "active",
    };
    const chapter = {
      id: "version-c",
      kind: "chapter",
      title: "Version chapter",
      status: "drafting",
      order_index: 1,
      parent_id: null,
    };
    const cursor = "created at&version/+=2";
    const calls: string[] = [];

    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith("/projects")) return response([project]);
      if (url.endsWith("/workspace/navigation")) return response({ project, nodes: [chapter] });
      if (url.endsWith("/chapters/version-c")) {
        return response({ content: "saved draft", contract: {}, current_version_id: "v-new", revision: 1 });
      }
      if (url.includes("/chapters/version-c/versions/page?")) {
        const parsed = new URL(url, "http://local.test");
        const before = parsed.searchParams.get("before");
        if (before === cursor) {
          return response({
            items: [versionItem("v-old", chapter.id, "旧元数据", "2026-08-29")],
            next_cursor: null,
          });
        }
        return response({
          items: [versionItem("v-new", chapter.id, "最新元数据", "2026-08-30")],
          next_cursor: cursor,
        });
      }
      if (/\/chapters\/version-c\/versions(?:\?|$)/.test(url)) {
        throw new Error(`legacy versions endpoint requested: ${url}`);
      }
      if (url.includes("/ai/jobs/page?")) return response({ items: [], next_cursor: null });
      if (url.endsWith("/summary")) return response(null);
      if (url.endsWith("/settings/model")) {
        return response({ mode: "demo", model: "", base_url: "", has_api_key: false, external_consent: false });
      }
      if (url.endsWith("/rag/health")) return response({ vectors: "disabled", documents: 0 });
      if (url.endsWith("/progress")) {
        return response({
          current_words: 0,
          target_words: 1000,
          completion_ratio: 0,
          chapter_count: 1,
          completed_chapters: 0,
          daily_goal: 100,
        });
      }
      const page = emptyLibraryPage(url);
      if (page) return response(page);
      return response([]);
    }));

    render(<AppProviders><App /></AppProviders>);
    await screen.findByLabelText("给 AI 的消息");

    await waitFor(() => {
      expect(calls.some((url) => url.endsWith("/chapters/version-c/versions/page?limit=50"))).toBe(true);
    });
    expect(calls.some((url) => /\/chapters\/version-c\/versions(?:\?|$)/.test(url))).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "查看正文" }));
    await userEvent.click(await screen.findByText("历史版本"));
    expect(await screen.findByText("最新元数据", { selector: "strong" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "加载更多" }));
    expect(await screen.findByText("旧元数据", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("最新元数据", { selector: "strong" })).toBeInTheDocument();

    const versionPanel = screen.getByText("最新元数据", { selector: "strong" }).closest("section");
    expect(versionPanel).not.toBeNull();
    const versionList = within(versionPanel!).getByRole("list");
    expect(within(versionList).getAllByRole("listitem")).toHaveLength(2);
    const nextCall = calls.find((url) => url.includes("/versions/page?") && url.includes("before="));
    expect(nextCall).toBeDefined();
    expect(new URL(nextCall!, "http://local.test").searchParams.get("before")).toBe(cursor);
    expect(nextCall).not.toContain("before=created at&version/+=2");
  });

  it("shows a generic initial error and retries the first page", async () => {
    let attempts = 0;
    stubVersionStudio(() => {
      attempts += 1;
      return attempts === 1
        ? failedResponse()
        : response({ items: [versionItem("v-new", "version-error-c", "重试成功")], next_cursor: null });
    });
    await openVersionPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent("版本记录加载失败");
    expect(screen.getByRole("alert")).not.toHaveTextContent("sensitive backend detail");
    await userEvent.click(screen.getByRole("button", { name: "重试加载版本" }));
    expect(await screen.findByText("重试成功", { selector: "strong" })).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it("keeps loaded versions while a failed next page is retried", async () => {
    let nextAttempts = 0;
    stubVersionStudio((url) => {
      if (!new URL(url, "http://local.test").searchParams.has("before")) {
        return response({
          items: [versionItem("v-new", "version-error-c", "已加载版本")],
          next_cursor: "older cursor",
        });
      }
      nextAttempts += 1;
      return nextAttempts === 1
        ? failedResponse()
        : response({ items: [versionItem("v-old", "version-error-c", "重试旧版本")], next_cursor: null });
    });
    await openVersionPanel();
    expect(await screen.findByText("已加载版本", { selector: "strong" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "加载更多" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("加载更多版本失败");
    expect(screen.getByText("已加载版本", { selector: "strong" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试加载版本" }));
    expect(await screen.findByText("重试旧版本", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("已加载版本", { selector: "strong" })).toBeInTheDocument();
    expect(nextAttempts).toBe(2);
  });

  it("retries a failed refresh instead of attempting a nonexistent next page", async () => {
    let attempts = 0;
    stubVersionStudio(() => {
      attempts += 1;
      if (attempts === 1) {
        return response({
          items: [versionItem("v-cached", "version-error-c", "缓存版本")],
          next_cursor: null,
        });
      }
      if (attempts === 2) return failedResponse();
      return response({
        items: [versionItem("v-refreshed", "version-error-c", "刷新恢复")],
        next_cursor: null,
      });
    });
    await openVersionPanel();
    expect(await screen.findByText("缓存版本", { selector: "strong" })).toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText("章节正文"));
    await userEvent.type(screen.getByLabelText("章节正文"), "updated draft");
    await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("版本记录刷新失败");
    expect(screen.getByText("缓存版本", { selector: "strong" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "加载更多" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试加载版本" }));

    expect(await screen.findByText("刷新恢复", { selector: "strong" })).toBeInTheDocument();
    expect(attempts).toBe(3);
  });
});
