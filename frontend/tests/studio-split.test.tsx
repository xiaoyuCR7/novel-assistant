import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { AppShell } from "../src/components/AppShell";
import { ProjectSwitcher } from "../src/features/projects/ProjectSwitcher";

beforeEach(() => localStorage.clear());

it("switches explicitly between novels", async () => {
  const onChange = vi.fn();
  render(
    <ProjectSwitcher
      projects={[
        { id: "a", title: "雾城来信" },
        { id: "b", title: "群星档案" },
      ]}
      activeId="a"
      onChange={onChange}
      onCreate={vi.fn()}
    />,
  );
  await userEvent.selectOptions(
    screen.getByRole("combobox", { name: "当前小说项目" }),
    "b",
  );
  expect(onChange).toHaveBeenCalledWith("b");
});

it("resizes materials with keyboard and stores width per project", async () => {
  render(
    <AppShell
      projectId="a"
      projectTitle="雾城来信"
      nodes={[]}
      selectedNodeId={null}
      onSelectNode={vi.fn()}
      inspector={<p>前文参考</p>}
    >
      <p>正文画布</p>
    </AppShell>,
  );
  const splitter = screen.getByRole("separator", {
    name: "调整素材侧边栏宽度",
  });
  splitter.focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(splitter).toHaveAttribute("aria-valuenow", "308");
  expect(localStorage.getItem("studio:a:materials-width")).toBe("308");
  await userEvent.keyboard("{End}");
  expect(splitter).toHaveAttribute("aria-valuenow", "420");
  expect(screen.getByRole("main")).toHaveClass("manuscript-paper");
});
