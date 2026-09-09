import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { expect, it, vi } from "vitest";

import { ProjectPreparation } from "../src/features/preparation/ProjectPreparation";
import { ChatWorkspace } from "../src/features/ai/ChatWorkspace";
import { ApiError } from "../src/lib/api";
import type { ProjectPreparationState } from "../src/lib/types";

const question = {
  id: "q-1",
  fingerprint: "f-1",
  round: 1 as const,
  setting_key: "world.rules",
  category: "world_rules",
  question: "这个世界有哪些不能打破的规则？",
  rationale: "避免后期解法破坏可信度。",
  impact_areas: ["连续性", "冲突"],
  priority: "high" as const,
  answer_format: "long_text" as const,
  options: [],
  source_ids: ["project:core"],
  origin: "template" as const,
  status: "open" as const,
  answer: null,
  canon_fact_id: null,
  updated_at: null,
};

function state(overrides: Partial<ProjectPreparationState> = {}): ProjectPreparationState {
  return {
    project_id: "p-1",
    revision: 0,
    status: "not_started",
    round: 0,
    source_hash: "",
    questions: [],
    generation_job_ids: [],
    actionable_job_id: null,
    impact_notice: null,
    answered_count: 0,
    unresolved_count: 0,
    unresolved_high_count: 0,
    ...overrides,
  };
}

function setup(client: Record<string, ReturnType<typeof vi.fn>>) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={cache}>{children}</QueryClientProvider>
  );
  render(<ProjectPreparation projectId="p-1" client={client as never} />, { wrapper });
}

it("does not call AI until the author explicitly asks for supplementation", async () => {
  const posts: string[] = [];
  let current = state();
  const client = {
    preparation: vi.fn(async () => current),
    initializePreparation: vi.fn(async () => {
      posts.push("initialize");
      current = state({ revision: 1, status: "in_progress", round: 1, questions: [question], unresolved_high_count: 1 });
      return current;
    }),
    analyzePreparation: vi.fn(async () => {
      posts.push("analyze");
      return { id: "job-1", status: "queued", task_type: "preparation_analysis" };
    }),
    job: vi.fn(async () => ({ id: "job-1", status: "running", task_type: "preparation_analysis" })),
  };
  setup(client);
  await userEvent.click(await screen.findByRole("button", { name: "开始整理" }));
  expect(posts).toEqual(["initialize"]);
  await userEvent.click(await screen.findByRole("button", { name: "智能补充缺失设定" }));
  expect(posts).toEqual(["initialize", "analyze"]);
  expect(screen.getByRole("button", { name: "智能补充缺失设定" })).toBeDisabled();
});

it("marks the active question, exposes its status, and caps free-text input at the server limit", async () => {
  const client = {
    preparation: vi.fn(async () =>
      state({
        revision: 1,
        status: "in_progress",
        round: 1,
        questions: [question],
        unresolved_count: 1,
        unresolved_high_count: 1,
      }),
    ),
  };
  setup(client);
  expect(await screen.findByRole("button", { name: "打开第 1 个问题，待回答" })).toHaveAttribute(
    "aria-current",
    "step",
  );
  expect(screen.getByLabelText("作者回答")).toHaveAttribute("maxlength", "10000");
});

it("reconnects to the latest actionable preparation job after remount", async () => {
  const client = {
    preparation: vi.fn(async () =>
      state({
        revision: 2,
        status: "in_progress",
        round: 1,
        questions: [question],
        actionable_job_id: "job-active",
        unresolved_count: 1,
        unresolved_high_count: 1,
      }),
    ),
    job: vi.fn(async () => ({
      id: "job-active",
      status: "running",
      task_type: "preparation_analysis",
      context_snapshot: {},
      result: {},
      allowed_actions: ["cancel"],
    })),
  };
  setup(client);
  expect(await screen.findByText("智能补充正在后台分析，不影响继续回答。")).toBeInTheDocument();
  expect(client.job).toHaveBeenCalledWith("job-active");
});

it("offers recovery for a rediscovered preparation job", async () => {
  const recoveryJob = {
    id: "job-recovery",
    status: "recovery_required",
    task_type: "preparation_analysis",
    control_revision: 3,
    recovery_reason: "stage_failed",
    context_snapshot: {},
    result: {},
    allowed_actions: ["resume", "cancel"],
  };
  const client = {
    preparation: vi.fn(async () =>
      state({
        revision: 2,
        status: "in_progress",
        round: 1,
        questions: [question],
        actionable_job_id: recoveryJob.id,
        unresolved_count: 1,
        unresolved_high_count: 1,
      }),
    ),
    job: vi.fn(async () => recoveryJob),
    resumeJob: vi.fn(async () => ({ ...recoveryJob, status: "queued", allowed_actions: ["cancel"] })),
    cancelJob: vi.fn(),
  };
  setup(client);
  await userEvent.click(await screen.findByRole("button", { name: "恢复智能补充" }));
  expect(client.resumeJob).toHaveBeenCalledWith(
    recoveryJob.id,
    { expected_control_revision: 3, confirm_unknown: false },
    expect.stringMatching(/^preparation-resume-/),
  );
});

it("adopts the server revision after a conflict without losing the local draft", async () => {
  const current = state({
    revision: 2,
    status: "in_progress",
    round: 1,
    questions: [question],
    unresolved_count: 1,
    unresolved_high_count: 1,
  });
  const answerPreparation = vi
    .fn()
    .mockRejectedValueOnce(new ApiError(409, "资料已被其他窗口更新", current, "revision_conflict"))
    .mockResolvedValueOnce({
      ...current,
      revision: 3,
      status: "completed",
      questions: [{ ...question, status: "answered", answer: "魔法必然消耗寿命" }],
      answered_count: 1,
      unresolved_count: 0,
      unresolved_high_count: 0,
    });
  const client = {
    preparation: vi.fn(async () => ({ ...current, revision: 1 })),
    answerPreparation,
  };
  setup(client);
  const input = await screen.findByLabelText("作者回答");
  await userEvent.type(input, "魔法必然消耗寿命");
  await userEvent.click(screen.getByRole("button", { name: "保存回答" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("资料已被其他窗口更新");
  expect(input).toHaveValue("魔法必然消耗寿命");

  await userEvent.click(screen.getByRole("button", { name: "保存回答" }));
  expect(answerPreparation).toHaveBeenLastCalledWith(
    question.id,
    expect.objectContaining({ revision: 2, answer: "魔法必然消耗寿命" }),
  );
});

it("does not offer follow-up while any medium-priority question remains unresolved", async () => {
  const mediumQuestion = { ...question, id: "q-medium", priority: "medium" as const };
  const client = {
    preparation: vi.fn(async () =>
      state({
        revision: 2,
        status: "in_progress",
        round: 1,
        questions: [mediumQuestion],
        generation_job_ids: ["analysis-job"],
        unresolved_count: 1,
        unresolved_high_count: 0,
      }),
    ),
  };
  setup(client);
  expect(await screen.findByText(/待明确 1/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "进行一次追问检查" })).not.toBeInTheDocument();
});

it("preserves the author's draft when saving fails", async () => {
  const client = {
    preparation: vi.fn(async () => state({ revision: 1, status: "in_progress", round: 1, questions: [question], unresolved_high_count: 1 })),
    answerPreparation: vi.fn(async () => { throw new Error("网络中断"); }),
  };
  setup(client);
  const input = await screen.findByLabelText("作者回答");
  await userEvent.type(input, "魔法必然消耗寿命");
  await userEvent.click(screen.getByRole("button", { name: "保存回答" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("网络中断");
  expect(input).toHaveValue("魔法必然消耗寿命");
});

it("shows a persistent manuscript impact notice until acknowledged", async () => {
  let current = state({
    revision: 3,
    status: "in_progress",
    round: 1,
    questions: [question],
    unresolved_high_count: 1,
    impact_notice: { fact_ids: ["fact-1"], chapter_count: 12, created_at: "2026-09-03T00:00:00" },
  });
  const client = {
    preparation: vi.fn(async () => current),
    acknowledgePreparationImpact: vi.fn(async () => {
      current = { ...current, revision: 4, impact_notice: null };
      return current;
    }),
  };
  setup(client);
  expect(await screen.findByRole("alert")).toHaveTextContent("12 章正文");
  await userEvent.click(screen.getByRole("button", { name: "我已知晓，关闭提醒" }));
  await waitFor(() => expect(screen.queryByText(/12 章正文/)).not.toBeInTheDocument());
});

it("keeps the composer draft when preparation pauses prose generation", async () => {
  const onSend = vi.fn(async () => undefined);
  const beforeSend = vi.fn(async () => false);
  render(
    <ChatWorkspace
      chapterTitle="第一章"
      hasChapter
      jobs={[]}
      running={false}
      onSend={onSend}
      beforeSend={beforeSend}
      onAccept={vi.fn()}
      onOpenManuscript={vi.fn()}
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: "续写" }));
  await userEvent.type(screen.getByLabelText("给 AI 的消息"), "继续雨夜送信");
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(beforeSend).toHaveBeenCalledWith("continue");
  expect(onSend).not.toHaveBeenCalled();
  expect(screen.getByLabelText("给 AI 的消息")).toHaveValue("继续雨夜送信");
});
