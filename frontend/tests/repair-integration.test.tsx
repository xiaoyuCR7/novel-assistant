import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../src/app/App";
import { emptyLibraryPage, emptyWorkspaceView } from './library-fixtures';
import { AppProviders } from "../src/app/providers";

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

it("refreshes only chat dependencies and connects feedback and confirmed rules", async () => {
  const project = { id: "integration-p", title: "test novel", premise: "", genre: "", status: "active", target_words: 1000, daily_goal: 100 };
  const job = { id: "integration-j", chapter_id: null, status: "succeeded", task_type: "chat", instructions: "request", context_snapshot: { fragments: [] }, result: { reply: "reply" } };
  let sent = false;
  const requests: Array<{ url: string; method: string; body: object }> = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), method = init?.method ?? "GET";
    requests.push({ url, method, body: JSON.parse(String(init?.body ?? "{}")) });
    let data: unknown = emptyLibraryPage(url) ?? emptyWorkspaceView(url) ?? [];
    if (url.endsWith("/projects")) data = [project];
    if (url.endsWith('/workspace/navigation')) data = { project, nodes: [] };
    if (url.endsWith("/settings/model")) data = { mode: "demo", base_url: "", model: "", has_api_key: false, external_consent: false, context_capacity: 32768, output_token_budget: 4096 };
    if (url.endsWith("/progress")) data = { current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 0, completed_chapters: 0, daily_goal: 100 };
    if (url.endsWith('/ai/jobs') && method === 'POST') { sent = true; data = job; }
    if (url.endsWith('/ai/jobs/preflight')) data = { required_input_tokens: 1000, token_budget: 28672,
      output_token_budget: 4096, context_capacity: 32768, effective_input_limit: 28672,
      can_fit: true, estimated: true, scope: 'first-stage-hard-only', message: '仅首阶段估算。' };
    if (url.includes('/ai/jobs/page?')) {
      const { result, context_snapshot, ...task } = job;
      data = { items: sent ? [{ ...task, preview: result.reply }] : [], next_cursor: null };
    }
    if (url.endsWith('/ai/jobs/integration-j')) data = job;
    if (url.endsWith("/styles/active-context")) data = { rules: [{ id: "r1", status: "confirmed", instruction: "active rule from server" }] };
    return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }));
  }));
  render(<AppProviders><App /></AppProviders>);
  const send = await screen.findByRole("button", { name: "发送消息" });
  await waitFor(() => expect(send).toBeEnabled());
  requests.length = 0;
  await userEvent.click(send);
  await screen.findByText("reply", { selector: "p" });
  expect(requests.some(r => r.url.endsWith("/workspace"))).toBe(false);
  expect(requests.some(r => r.url.endsWith("/library"))).toBe(false);
  await userEvent.click(screen.getByText("评价这次回复"));
  await userEvent.type(screen.getByLabelText("修改原因"), "Keep it concise");
  await userEvent.click(screen.getByRole("button", { name: "提交评价与修正" }));
  await waitFor(() => expect(requests.find(r => r.url.endsWith("/feedback"))?.body).toEqual(expect.objectContaining({ job_id: job.id, project_id: project.id, original_text: "reply" })));
  await userEvent.click(screen.getByRole("button", { name: "资料库" }));
  await userEvent.selectOptions(screen.getByLabelText("资料工作区"), "style");
  expect(await screen.findByText("active rule from server")).toBeVisible();
});
