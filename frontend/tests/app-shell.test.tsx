import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/AppShell";

const nodes = [
  {
    id: "volume-1",
    kind: "volume" as const,
    title: "第一卷：无主之信",
    status: "active",
  },
  {
    id: "chapter-1",
    kind: "chapter" as const,
    title: "雾中投递",
    status: "drafting",
  },
  {
    id: "scene-1",
    kind: "scene" as const,
    title: "钟楼下的交接",
    status: "planned",
  },
];

describe("AppShell", () => {
  it("gives the manuscript, book spine, and contextual inspector distinct landmarks", () => {
    render(
      <AppShell
        projectTitle="雾城来信"
        nodes={nodes}
        selectedNodeId="chapter-1"
        onSelectNode={vi.fn()}
        inspector={<p>本章相关事实</p>}
      >
        <article>
          <h1>雾中投递</h1>
          <p>章节正文画布</p>
        </article>
      </AppShell>,
    );

    expect(screen.getByRole("banner")).toHaveTextContent("小说导演台");
    expect(
      screen.getByRole("navigation", { name: "书脊轨道" }),
    ).toHaveTextContent("第一卷：无主之信");
    expect(screen.getByRole("main")).toHaveTextContent("章节正文画布");
    expect(
      screen.getByRole("complementary", { name: "上下文检查器" }),
    ).toHaveTextContent("本章相关事实");
    expect(screen.getByRole("button", { name: "雾中投递" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("lets narrow-screen users open and close both side panels", async () => {
    const user = userEvent.setup();
    render(
      <AppShell
        projectTitle="雾城来信"
        nodes={nodes}
        selectedNodeId="chapter-1"
        onSelectNode={vi.fn()}
        inspector={<p>检查器</p>}
      >
        <p>正文</p>
      </AppShell>,
    );

    const structureToggle = screen.getByRole("button", {
      name: "切换故事结构",
    });
    const inspectorToggle = screen.getByRole("button", {
      name: "切换上下文检查器",
    });
    expect(structureToggle).toHaveAttribute("aria-expanded", "false");
    expect(inspectorToggle).toHaveAttribute("aria-expanded", "false");

    await user.click(structureToggle);
    expect(structureToggle).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("button", { name: "关闭故事结构" }));
    expect(structureToggle).toHaveAttribute("aria-expanded", "false");
    await user.click(inspectorToggle);
    expect(inspectorToggle).toHaveAttribute("aria-expanded", "true");

    await user.click(screen.getByRole("button", { name: "关闭上下文检查器" }));
    expect(structureToggle).toHaveAttribute("aria-expanded", "false");
    expect(inspectorToggle).toHaveAttribute("aria-expanded", "false");
  });
});
