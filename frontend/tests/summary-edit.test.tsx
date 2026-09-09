import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { it, expect, vi } from "vitest";
import { ChapterSummaryPanel } from "../src/features/writing/ChapterSummaryPanel";

it("lets the author correct structured continuity state as well as the recap", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(
    <ChapterSummaryPanel
      summary={{
        id: "s",
        chapter_id: "c",
        version_id: "v",
        title: "第一章",
        recap: "章总结",
        details: { character_states: ["在码头"], end_state: "夜晚" },
        origin: "ai_generated",
        provider: "demo",
        status: "valid",
        revision: 3,
        content_hash: "hash",
      }}
      onSave={save}
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: "编辑总结" }));
  await userEvent.click(screen.getByText("修订结构化记录"));
  await userEvent.clear(screen.getByLabelText("人物状态"));
  await userEvent.type(screen.getByLabelText("人物状态"), "已到钟楼");
  await userEvent.click(screen.getByRole("button", { name: "保存总结" }));
  expect(save).toHaveBeenCalledWith(
    "章总结",
    3,
    expect.objectContaining({ character_states: ["已到钟楼"] }),
    "s",
  );
});

it("keeps internal content-check metadata outside the editable summary form", async () => {
  render(
    <ChapterSummaryPanel
      summary={{
        id: "checked",
        chapter_id: "chapter",
        version_id: "version",
        title: "第一章",
        recap: "章总结",
        details: {
          character_states: [],
          end_state: "夜晚",
          content_check: { version: 1, findings: [], observations: [] },
        },
        origin: "ai_generated",
        provider: "demo",
        status: "valid",
        revision: 1,
        content_hash: "hash",
      }}
      onSave={async () => undefined}
    />,
  );

  expect(screen.getByText("章总结")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "编辑总结" }));
  expect(screen.getByText("修订结构化记录")).toBeVisible();
  expect(screen.queryByText(/content_check/)).not.toBeInTheDocument();
});
