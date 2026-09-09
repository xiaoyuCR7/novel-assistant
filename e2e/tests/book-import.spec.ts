import { expect, test } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { resetToEmptyWorkspace } from "./helpers/workspace";

const fixtureDirectory = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "fixtures",
  "import-book",
);

test("作者导入未完成小说后可从断点继续并检索原始资料", async ({ page, request }) => {
  await resetToEmptyWorkspace(request);
  await page.goto("/");
  await expect(page.getByLabel("小说名称")).toBeVisible();
  await expect(page.getByRole("button", { name: "导入已有小说" })).toBeVisible();
  await page.getByRole("button", { name: "导入已有小说" }).click();

  const wizard = page.getByRole("dialog", { name: "导入已有小说" });
  await expect(wizard).toBeVisible();
  await wizard.getByLabel("小说文件夹").setInputFiles(fixtureDirectory);
  await expect(wizard.getByRole("status")).toContainText("已选择 5 个文件");
  await wizard.getByRole("button", { name: "下一步：检查章节" }).click();

  const characterCategory = wizard.getByLabel(/资料\/人物\.md 分类$/);
  await expect(characterCategory).toHaveValue("character");
  await characterCategory.selectOption("other");
  await expect(characterCategory).toHaveValue("other");
  await characterCategory.selectOption("character");
  await expect(characterCategory).toHaveValue("character");

  const chapterReview = wizard.getByRole("region", { name: "章节顺序" });
  const chapterNames = chapterReview.getByRole("textbox");
  await expect(chapterNames).toHaveCount(2);
  await expect(chapterNames.nth(0)).toHaveValue("第一章");
  await expect(chapterNames.nth(1)).toHaveValue("第二章");
  await wizard.getByRole("button", { name: "下一步：确认续写点" }).click();

  await wizard.getByLabel("已完成到").selectOption({ label: "第一章" });
  await wizard.getByLabel("当前未完成章节").selectOption({ label: "第二章" });
  await wizard.getByLabel("下一步写作目标").fill("从第二章断点继续调查失踪信件");
  await wizard.getByLabel("我已确认续写边界").check();
  await wizard.getByRole("button", { name: "下一步：创建项目" }).click();

  await wizard.getByLabel("小说名称").fill("失踪信件续写验收");
  await expect(wizard.getByRole("region", { name: "导入摘要" }))
    .toContainText("从第二章断点继续调查失踪信件");
  await wizard.getByRole("button", { name: "创建并导入小说" }).click();
  await expect(wizard).not.toBeVisible();
  await expect(page.locator(".connection-strip")).toContainText("离线演示 · 回复为示例内容");

  const spine = page.getByRole("navigation", { name: "书脊轨道" });
  const firstChapter = spine.getByRole("button", { name: "第一章", exact: true });
  const secondChapter = spine.getByRole("button", { name: "第二章", exact: true });
  await expect(firstChapter).toContainText("已完成");
  await expect(secondChapter).toContainText("创作中");
  await secondChapter.click();
  await page.getByRole("button", { name: "查看正文", exact: true }).click();
  const manuscript = page.getByLabel("章节正文");
  await expect(manuscript).toBeEditable();
  await expect(manuscript).toHaveValue(/准备从断点继续调查/);
  await manuscript.fill(`${await manuscript.inputValue()}\n顾临在页边写下了新的调查线索。`);
  await expect(manuscript).toHaveValue(/新的调查线索/);

  await page.getByRole("button", { name: "参考资料", exact: true }).click();
  const materials = page.getByRole("dialog", { name: "参考资料" });
  await materials.getByLabel("搜索本项目素材").fill("雾钟每天只能敲响七次");
  const worldSource = materials.locator(".material-row").filter({ hasText: "世界观" });
  await expect(worldSource).toBeVisible();
  await worldSource.click();
  await expect(page.getByText(/资料\/世界观\.md/)).toBeVisible();
  await page.getByRole("dialog", { name: "素材详情" })
    .getByRole("button", { name: "关闭对话框" }).click();

  await page.getByRole("button", { name: /查看导入分析|审核待确认记忆/ }).click();
  const analysis = page.getByRole("dialog", { name: "导入分析与记忆审核" });
  await expect(analysis).toBeVisible();
  await expect(analysis.getByLabel("导入分析进度")).toBeVisible();
  await analysis.getByRole("button", { name: "关闭" }).click();
  await expect(manuscript).toBeEditable();
  await expect(manuscript).toHaveValue(/新的调查线索/);
  await expect(page.getByLabel("给 AI 的消息")).toBeEditable();
});
