import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "../src/app/App";
import { RagStatus } from "../src/features/ai/RagStatus";

function json(data: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(data), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function providers(children: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

it.each([
  ["disabled", "未配置向量模型，关键词检索仍可用"],
  ["needs_rebuild", "向量索引待重建，关键词检索仍可用"],
  ["ready", "本机向量索引已就绪"],
  ["degraded", "向量检索暂时不可用，关键词检索仍可用"],
])("renders truthful vector state %s", async (state, wording) => {
  vi.stubGlobal("fetch", vi.fn(() => json({ vectors: state, documents: 2 })));
  providers(<RagStatus projectId={`project-${state}`} />);

  expect(await screen.findByText(wording)).toBeVisible();
  expect(document.body).not.toHaveTextContent("enabled");
});

const project = {
  id: "p",
  title: "Novel",
  premise: "",
  genre: "",
  target_words: 1000,
  daily_goal: 100,
  status: "active",
};
const chapter = {
  id: "chapter",
  title: "Chapter",
  kind: "chapter",
  status: "drafting",
};
const entity = {
  id: "entity-1",
  type: "entity",
  title: "人物",
  content: "人物资料",
  preview: "人物资料",
  revision: 1,
  is_pinned: false,
  deleted_at: null,
  purge_after: null,
  record: {},
};
const canon = {
  ...entity,
  id: "canon-1",
  type: "canon",
  title: "保留事实",
  content: "事实仍由作者保留",
  preview: "事实仍由作者保留",
};
const relation = {
  ...entity,
  id: "relation-1",
  type: "relation",
  title: "保留关系",
  content: "关系仍由作者保留",
  preview: "关系仍由作者保留",
};

function studio(initiallyDeleted = false, reactivatesDependents = true) {
  let deleted = initiallyDeleted;
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const parsed = new URL(url, "http://local");
      const path = parsed.pathname;
      requests.push(url);
      if (path.endsWith("/api/v1/projects")) return json([project]);
      if (path.endsWith("/workspace/navigation"))
        return json({ project, nodes: [chapter] });
      if (path.endsWith("/workspace/views/library"))
        return json({ memory_conflicts: [] });
      if (path.endsWith("/library/page")) {
        const independent =
          parsed.searchParams.get("category") === "summary" ||
          parsed.searchParams.get("pinned") === "true";
        const items = independent
          ? []
          : [canon, relation, ...(deleted ? [] : [entity])];
        return json({
          items,
          total: items.length,
          counts: { entity: deleted ? 0 : 1, canon: 1, relation: 1 },
          next_cursor: null,
        });
      }
      if (path.endsWith("/trash/page")) {
        const trashed = deleted
          ? [{ ...entity, revision: 2, deleted_at: "2026-09-01T00:00:00Z", purge_after: "2099-09-30T00:00:00Z" }]
          : [];
        return json({ items: trashed, total: trashed.length, counts: {}, next_cursor: null });
      }
      if (path.endsWith("/library/entity/entity-1") && init?.method === "DELETE") {
        deleted = true;
        return json({
          ...entity,
          revision: 2,
          deleted_at: "2026-09-01T00:00:00Z",
          purge_after: "2099-09-30T00:00:00Z",
          inactive_dependents: [
            { type: "canon", id: "canon-1", reason: "entity_deleted" },
            { type: "relation", id: "relation-1", reason: "entity_deleted" },
          ],
        });
      }
      if (path.endsWith("/trash/entity/entity-1/restore")) {
        deleted = false;
        return json({
          ...entity,
          revision: 3,
          reactivated_dependents: reactivatesDependents
            ? [
                { type: "canon", id: "canon-1" },
                { type: "relation", id: "relation-1" },
              ]
            : [],
        });
      }
      if (path.endsWith("/library/entity/entity-1")) return json(entity);
      if (path.endsWith("/chapters/chapter"))
        return json({ content: "saved", revision: 1, contract: {}, current_version_id: null });
      if (path.endsWith("/summary")) return json(null);
      if (path.endsWith("/ai/jobs/page")) return json({ items: [], next_cursor: null });
      if (path.endsWith("/settings/model"))
        return json({ mode: "demo", model: "", output_token_budget: 4096, context_capacity: 131072 });
      if (path.endsWith("/api/v1/health")) return json({ ai_provider: "demo" });
      if (path.endsWith("/rag/health")) return json({ vectors: "disabled", documents: 3 });
      if (path.endsWith("/progress"))
        return json({ current_words: 0, target_words: 1000, daily_goal: 100, completion_ratio: 0, chapter_count: 1, completed_chapters: 0 });
      if (path.endsWith("/conflicts")) return json([]);
      if (path.includes("/versions/page")) return json({ items: [], next_cursor: null });
      throw new Error(`Unexpected request: ${url}`);
    }),
  );
  providers(<App />);
  return { requests };
}

it("reports retained inactive dependencies after deleting an entity", async () => {
  const app = studio();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "参考资料" }));
  const entityTitle = await screen.findByText("人物", { selector: "strong" });
  await user.click(entityTitle.closest("button")!);
  await user.click(await screen.findByRole("button", { name: "编辑素材" }));
  await user.click(screen.getByRole("button", { name: "移到回收站" }));
  await user.click(screen.getByRole("button", { name: "确认移到回收站" }));

  expect(
    await screen.findByText("依赖实体已删除，暂不可用于检索/生成；作者资料仍保留，恢复实体后会重新启用。"),
  ).toBeVisible();
  expect(screen.getByText("确认事实 · canon-1")).toBeVisible();
  expect(screen.getByText("人物关系 · relation-1")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "参考资料" }));
  expect(await screen.findByRole("button", { name: /保留事实/ })).toBeVisible();
  expect(screen.getByRole("button", { name: /保留关系/ })).toBeVisible();
  expect(app.requests.some((url) => url.includes("/library/search"))).toBe(false);
});

it("reports which retained dependencies reactivate after restoring an entity", async () => {
  studio(true);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "资料库" }));
  await user.click(screen.getAllByRole("button", { name: "回收站" })[0]);
  await user.click(await screen.findByRole("button", { name: "恢复人物" }));

  expect(await screen.findByText("依赖实体已恢复，以下作者资料已重新加入检索/生成。"))
    .toBeVisible();
  expect(screen.getByText("确认事实 · canon-1")).toBeVisible();
  expect(screen.getByText("人物关系 · relation-1")).toBeVisible();
});

it("does not claim retrieval reactivation when restore reports no dependents", async () => {
  studio(true, false);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "资料库" }));
  await user.click(screen.getAllByRole("button", { name: "回收站" })[0]);
  await user.click(await screen.findByRole("button", { name: "恢复人物" }));

  const notice = await screen.findByRole("status");
  expect(notice).toHaveTextContent("已恢复");
  expect(notice).not.toHaveTextContent("重新加入检索");
});
