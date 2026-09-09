import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DirectorConsole } from "../src/features/ai/DirectorConsole";
import { AssetLibrary } from "../src/features/assets/AssetLibrary";
import { ConflictCenter } from "../src/features/conflicts/ConflictCenter";
import { FeedbackPanel } from "../src/features/feedback/FeedbackPanel";
import { ProgressPulse } from "../src/features/progress/ProgressPulse";
import { StyleLab } from "../src/features/style/StyleLab";

describe("AI, feedback, and multimodal workspaces", () => {
  it("previews sources and keeps generated text as a candidate until accepted", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn().mockResolvedValue(undefined);
    const onAccept = vi.fn().mockResolvedValue(undefined);
    render(
      <DirectorConsole
        context={{
          token_budget: 2000,
          total_estimated_tokens: 320,
          fragments: [
            {
              source_type: "chapter_contract",
              source_id: "c1",
              reason: "本章硬约束",
              hard: true,
              estimated_tokens: 80,
            },
            {
              source_type: "canon_fact",
              source_id: "f1",
              reason: "人物相关事实",
              hard: false,
              estimated_tokens: 40,
            },
          ],
        }}
        job={{
          id: "j1",
          status: "succeeded",
          task_type: "full_chapter",
          result: {
            candidate_text: "雾从钟楼背后落下来。",
            stage_order: ["plan", "draft", "rewrite"],
          },
        }}
        running={false}
        onRun={onRun}
        onAccept={onAccept}
      />,
    );

    await user.click(screen.getByRole("button", { name: "查看本次上下文" }));
    expect(screen.getByText("本章硬约束")).toBeInTheDocument();
    expect(screen.getByText("人物相关事实")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "生成并统一重写" }));
    expect(onRun).toHaveBeenCalledWith("full_chapter", expect.any(String));
    await user.click(screen.getByRole("button", { name: "生成场景描写" }));
    expect(onRun).toHaveBeenCalledWith("scene_description", expect.any(String));
    expect(screen.getByText("雾从钟楼背后落下来。")).toBeInTheDocument();
    expect(onAccept).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: "接受候选并创建版本" }),
    );
    expect(onAccept).toHaveBeenCalledWith("j1");
  });

  it("submits a scored correction and manages candidate style rules", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    const onDisable = vi.fn().mockResolvedValue(undefined);
    render(
      <>
        <FeedbackPanel
          originalText="他点了点头，表示明白。"
          onSubmit={onSubmit}
        />
        <StyleLab
          candidates={[
            {
              id: "p1",
              instruction: "动作后不追加解释性尾句。",
              category: "啰嗦",
              status: "candidate",
            },
          ]}
          rules={[]}
          onConfirm={onConfirm}
          onDisable={onDisable}
        />
      </>,
    );

    await user.selectOptions(screen.getByLabelText("评分"), "2");
    await user.clear(screen.getByLabelText("修正后的文字"));
    await user.type(screen.getByLabelText("修正后的文字"), "他点头。");
    await user.type(
      screen.getByLabelText("修改原因"),
      "不要解释已经清楚的动作。",
    );
    await user.click(screen.getByRole("button", { name: "提交评价与修正" }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ rating: 2, corrected_text: "他点头。" }),
    );

    await user.click(screen.getByRole("button", { name: "确认风格规则" }));
    expect(onConfirm).toHaveBeenCalledWith("p1");
    await user.click(screen.getByRole("button", { name: "停用风格规则" }));
    expect(onDisable).toHaveBeenCalledWith("p1");
  });

  it("records a balanced conflict decision without changing manuscript", async () => {
    const user = userEvent.setup();
    const onDecide = vi.fn().mockResolvedValue(undefined);
    render(
      <ConflictCenter
        conflicts={[
          {
            id: "x1",
            code: "MOTIVATION_GAP",
            severity: "warning",
            message: "人物缺少进入码头的充分动机。",
            status: "open",
            options: [
              {
                id: "o1",
                mode: "conservative",
                title: "补足局部动机",
                benefit: "改动小",
                risk: "力度有限",
                ripple_effects: ["补写动作"],
              },
              {
                id: "o2",
                mode: "balanced",
                title: "提前私人代价",
                benefit: "因果更强",
                risk: "调整信息",
                ripple_effects: ["检查知识边界"],
              },
              {
                id: "o3",
                mode: "radical",
                title: "改写转折",
                benefit: "钩子强",
                risk: "影响伏笔",
                ripple_effects: ["重算伏笔"],
              },
            ],
          },
        ]}
        onDecide={onDecide}
        onResolveEntityState={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("radio", { name: "平衡：提前私人代价" }));
    await user.type(screen.getByLabelText("决定说明"), "保留场景顺序。");
    await user.click(screen.getByRole("button", { name: "记录解决决定" }));
    expect(onDecide).toHaveBeenCalledWith("x1", "o2", "保留场景顺序。");
  });

  it("retracts an explicit entity state instead of merely closing its conflict", async () => {
    const user = userEvent.setup();
    const onResolveEntityState = vi.fn().mockResolvedValue(undefined);
    render(
      <ConflictCenter
        conflicts={[{
          id: "state-conflict",
          code: "ENTITY_STATE_CONFLICT",
          severity: "error",
          message: "人物状态互相矛盾。",
          status: "open",
          options: [],
          entity_state_resolution: {
            conflicts: [{
              code: "ENTITY_STATE_CONFLICT",
              entity_id: "entity-1",
              key: "alive",
              state_ids: ["state-a", "state-b"],
              values: [true, false],
            }],
            states: [
              {
                id: "state-a",
                entity_id: "entity-1",
                data: { alive: true },
                valid_from_node_id: "chapter-1",
                valid_to_node_id: null,
                status: "confirmed",
                revision: 1,
              },
              {
                id: "state-b",
                entity_id: "entity-1",
                data: { alive: false },
                valid_from_node_id: "chapter-1",
                valid_to_node_id: null,
                status: "confirmed",
                revision: 3,
              },
            ],
          },
        }]}
        onDecide={vi.fn()}
        onResolveEntityState={onResolveEntityState}
      />,
    );

    await user.click(screen.getByRole("radio", { name: /撤回状态.*alive.*false/ }));
    await user.type(screen.getByLabelText("状态冲突处理说明"), "保留存活记录。");
    await user.click(screen.getByRole("button", { name: "撤回所选状态并重新检查" }));

    expect(onResolveEntityState).toHaveBeenCalledWith(
      "state-conflict", "state-b", 3, "保留存活记录。",
    );
  });

  it("generates project-linked concept art and shows progress as a quiet pulse", async () => {
    const user = userEvent.setup();
    const onGenerate = vi.fn().mockResolvedValue(undefined);
    render(
      <>
        <AssetLibrary
          assets={[
            {
              id: "a1",
              project_id: "p1",
              kind: "character",
              prompt: "雾城邮差",
              relative_path: "projects/p1/assets/a1.png",
              status: "ready",
              provider: "demo",
              model: "demo-image",
            },
          ]}
          onGenerate={onGenerate}
        />
        <ProgressPulse
          progress={{
            current_words: 24000,
            target_words: 120000,
            completion_ratio: 0.2,
            chapter_count: 12,
            completed_chapters: 3,
            daily_goal: 1500,
          }}
        />
      </>,
    );

    await user.selectOptions(screen.getByLabelText("素材类型"), "scene");
    await user.type(screen.getByLabelText("画面要求"), "第七码头在雾潮中显现");
    await user.click(screen.getByRole("button", { name: "生成概念图" }));
    expect(onGenerate).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "scene" }),
    );
    expect(screen.getByText("雾城邮差")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "雾城邮差" })).toHaveAttribute(
      "src",
      "/api/v1/projects/p1/assets/a1/file",
    );
    expect(screen.getByText("24,000 / 120,000 字")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "20",
    );
  });
});
