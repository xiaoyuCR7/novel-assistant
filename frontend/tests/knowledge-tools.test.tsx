import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { IdeaBoard } from "../src/features/ideas/IdeaBoard";
import { KnowledgeBase } from "../src/features/knowledge/KnowledgeBase";
import { StoryMap } from "../src/features/story/StoryMap";
import { StyleLab } from "../src/features/style/StyleLab";

describe("long-form knowledge tools", () => {
  it("captures loose inspiration with tags and source", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockResolvedValue(undefined);
    render(<IdeaBoard ideas={[]} onCreate={onCreate} />);

    await user.type(screen.getByLabelText("灵感标题"), "死者仍会收到回信");
    await user.type(
      screen.getByLabelText("灵感内容"),
      "每封信都在午夜改变收件人。",
    );
    await user.type(screen.getByLabelText("标签"), "世界观, 钩子");
    await user.type(screen.getByLabelText("来源"), "散步随记");
    await user.click(screen.getByRole("button", { name: "收进灵感匣" }));

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "死者仍会收到回信",
        tags: ["世界观", "钩子"],
        source: "散步随记",
      }),
    );
  });

  it("records confirmed canon and ordered timeline events", async () => {
    const user = userEvent.setup();
    const onCreateCanon = vi.fn().mockResolvedValue(undefined);
    const onCreateTimeline = vi.fn().mockResolvedValue(undefined);
    render(
      <KnowledgeBase
        canonFacts={[]}
        timeline={[]}
        onCreateCanon={onCreateCanon}
        onCreateTimeline={onCreateTimeline}
      />,
    );

    await user.type(screen.getByLabelText("事实关系"), "惧怕");
    await user.type(screen.getByLabelText("事实内容"), "钟声");
    await user.click(screen.getByRole("button", { name: "确认事实" }));
    expect(onCreateCanon).toHaveBeenCalledWith(
      expect.objectContaining({
        predicate: "惧怕",
        value: "钟声",
        status: "confirmed",
      }),
    );

    await user.type(screen.getByLabelText("事件标题"), "旧钟楼失火");
    await user.type(screen.getByLabelText("故事时间"), "雾历17年冬");
    await user.click(screen.getByRole("button", { name: "加入时间线" }));
    expect(onCreateTimeline).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "旧钟楼失火",
        story_time: "雾历17年冬",
      }),
    );
  });

  it("adds a foreshadowing promise to the narrative rail", async () => {
    const user = userEvent.setup();
    const onCreatePlot = vi.fn().mockResolvedValue(undefined);
    render(
      <StoryMap
        nodes={[]}
        plots={[]}
        selectedNodeId={null}
        onSelectNode={vi.fn()}
        onCreateNode={vi.fn()}
        onCreatePlot={onCreatePlot}
      />,
    );

    await user.selectOptions(
      screen.getByLabelText("情节类型"),
      "foreshadowing",
    );
    await user.type(screen.getByLabelText("情节标题"), "无名收件人");
    await user.type(
      screen.getByLabelText("叙事承诺"),
      "揭示第一封信其实写给主角",
    );
    await user.click(screen.getByRole("button", { name: "加入叙事轨" }));
    expect(onCreatePlot).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "foreshadowing", title: "无名收件人" }),
    );
  });

  it("creates an explicit style profile alongside learned preferences", async () => {
    const user = userEvent.setup();
    const onCreateProfile = vi.fn().mockResolvedValue(undefined);
    render(
      <StyleLab
        candidates={[]}
        rules={[]}
        profiles={[]}
        onConfirm={vi.fn()}
        onDisable={vi.fn()}
        onCreateProfile={onCreateProfile}
      />,
    );

    await user.type(screen.getByLabelText("风格方案名"), "冷雾短句");
    await user.selectOptions(screen.getByLabelText("叙事距离"), "close");
    await user.selectOptions(screen.getByLabelText("句式节奏"), "crisp");
    await user.type(screen.getByLabelText("禁用表达"), "命运的齿轮, 不禁");
    await user.click(screen.getByRole("button", { name: "保存并启用方案" }));
    expect(onCreateProfile).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "冷雾短句",
        is_active: true,
        config: expect.objectContaining({
          distance: "close",
          rhythm: "crisp",
          forbidden_phrases: ["命运的齿轮", "不禁"],
        }),
      }),
    );
  });
});
