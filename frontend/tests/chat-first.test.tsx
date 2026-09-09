import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { WorkspaceRail } from "../src/components/WorkspaceRail";
import { ChatWorkspace } from "../src/features/ai/ChatWorkspace";
import { ModelSettingsForm } from "../src/features/settings/ModelSettings";

it("keeps just three primary destinations", () => {
  render(<WorkspaceRail view="ai" onChange={vi.fn()} />);
  const nav = screen.getByRole("navigation", { name: "创作空间" });
  expect(within(nav).getAllByRole("button")).toHaveLength(3);
  expect(within(nav).getByRole("button", { name: "创作" })).toBeVisible();
  expect(within(nav).getByRole("button", { name: "设置" })).toBeVisible();
});

it("sends dialogue and keeps candidate acceptance explicit", async () => {
  const send = vi.fn().mockResolvedValue(undefined),
    accept = vi.fn().mockResolvedValue(undefined);
  render(
    <ChatWorkspace
      chapterTitle="第一章"
      hasChapter={true}
      jobs={[]}
      running={false}
      onSend={send}
      onAccept={accept}
      onOpenManuscript={vi.fn()}
    />,
  );
  await userEvent.type(screen.getByLabelText("给 AI 的消息"), "帮我讨论开场");
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(send).toHaveBeenCalledWith("chat", "帮我讨论开场");
  expect(accept).not.toHaveBeenCalled();
});

it("does not allow API save without disclosure consent or reveal a saved key", async () => {
  const save = vi.fn().mockResolvedValue({ mode: "api", base_url: "https://example.com/v1",
    model: "test", has_api_key: true, external_consent: true });
  render(
    <ModelSettingsForm
      value={{
        mode: "api",
        base_url: "https://example.com/v1",
        model: "test",
        has_api_key: true,
        external_consent: false,
      }}
      onSave={save}
      onTest={vi.fn()}
    />,
  );
  expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
  expect(screen.getByLabelText("API Key")).toHaveValue("");
  expect(screen.getByRole("button", { name: "保存设置" })).toBeDisabled();
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", { name: "保存设置" }));
  expect(save).toHaveBeenCalledWith(
    expect.objectContaining({ external_consent: true, api_key: "" }),
  );
});
