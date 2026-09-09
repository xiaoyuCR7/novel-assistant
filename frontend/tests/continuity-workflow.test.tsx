import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ChapterWorkspace } from "../src/features/writing/ChapterWorkspace";
import { DriftAlertCenter } from "../src/features/conflicts/DriftAlertCenter";

it("completes using the current unsaved manuscript", async () => {
  const complete = vi.fn().mockResolvedValue(undefined);
  render(
    <ChapterWorkspace
      title="第一章"
      document={{ content: "旧文", contract: {}, current_version_id: null }}
      saving={false}
      onSave={vi.fn()}
      onComplete={complete}
    />,
  );
  await userEvent.clear(screen.getByLabelText("章节正文"));
  await userEvent.type(screen.getByLabelText("章节正文"), "新版正文");
  await userEvent.click(screen.getByRole("button", { name: "完成本章" }));
  expect(complete).toHaveBeenCalledWith(
    expect.objectContaining({ content: "新版正文" }),
  );
});
it("requires a second author decision for severe alerts", async () => {
  const decide = vi.fn().mockResolvedValue(undefined);
  render(
    <DriftAlertCenter
      alerts={[
        {
          id: "x",
          severity: "severe",
          message: "提前揭示身份",
          status: "open",
        },
      ]}
      onDecide={decide}
    />,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "仍按作者决定继续" }),
  );
  expect(decide).not.toHaveBeenCalled();
  expect(
    screen.getByRole("dialog", { name: "确认忽略严重偏离" }),
  ).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "确认继续" }));
  expect(decide).toHaveBeenCalledWith("x", "accept", true);
});

it("preserves a dirty draft and its base revision when a background refresh arrives", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const original = {
    content: "初稿",
    contract: {},
    current_version_id: null,
    revision: 1,
  };
  const { rerender } = render(
    <ChapterWorkspace
      title="第一章"
      document={original}
      saving={false}
      onSave={save}
    />,
  );
  await userEvent.type(screen.getByLabelText("章节正文"), "未保存文字");
  rerender(
    <ChapterWorkspace
      title="第一章"
      document={{ ...original, content: "其他窗口的新正文", revision: 2 }}
      saving={false}
      onSave={save}
    />,
  );
  expect(screen.getByLabelText("章节正文")).toHaveValue("初稿未保存文字");
  await userEvent.click(screen.getByRole("button", { name: "保存工作副本" }));
  expect(save).toHaveBeenCalledWith(
    expect.objectContaining({ content: "初稿未保存文字", revision: 1 }),
  );
});
