import { expect, test, type Page } from "@playwright/test";

async function expectStudioReady(page: Page, projectTitle: string) {
  await expect(page.getByLabel("当前小说项目").locator("option:checked")).toHaveText(projectTitle);
  await expect(page.getByRole("button", { name: "发送消息", exact: true })).toBeEnabled();
}

test("author creates, structures, writes, generates, illustrates, versions, and exports", async ({ page }) => {
  await page.goto("/");
  // The suite shares a temporary server: other journeys may already have novels.
  const newNovel = page.getByRole("button", { name: "创建新小说" });
  await expect(page.getByLabel("小说名称").or(newNovel)).toBeVisible();
  if (await newNovel.isVisible()) await newNovel.click();
  await page.getByLabel("小说名称").fill("雾城来信");
  await page.getByLabel("核心前提").fill("失忆邮差替死者送出最后一批信。");
  await page.getByLabel("类型").fill("奇幻悬疑");
  await page.getByRole("button", { name: "新建空白小说" }).click();

  await expect(page.getByLabel("给 AI 的消息")).toBeVisible();
  await expectStudioReady(page, "雾城来信");
  await page.getByRole("button", { name: "资料库", exact: true }).click();
  await page.getByLabel("资料工作区").selectOption("story");
  await page.getByLabel("节点类型").selectOption("volume");
  await page.getByLabel("节点标题").fill("第一卷：雾潮");
  await page.getByRole("button", { name: "加入故事地图" }).click();
  await page.getByRole("button", { name: "第一卷：雾潮" }).last().click();
  await page.getByLabel("节点类型").selectOption("chapter");
  await page.getByLabel("节点标题").fill("第一章：无主邮袋");
  await page.getByRole("button", { name: "加入故事地图" }).click();
  await page.getByRole("complementary", { name: "小说导航" })
    .getByRole("button", { name: "第一章：无主邮袋", exact: true }).click();
  await page.getByRole("button",{name:"查看正文"}).click();

  await page.getByLabel("本章目的").fill("让林渡接下不可退回的邮袋");
  await page.getByLabel("章节正文").fill("夜班铃响了三次，门才向里开了一条缝。");
  await page.getByRole("button", { name: "保存工作副本" }).click();
  await expect(page.getByText("工作副本已保存")).toBeVisible();
  await expect(page.getByRole("button", { name: "保存工作副本", exact: true })).toBeEnabled();

  await page.getByRole("button", { name: "收起正文" }).click();
  await page.getByRole("button", { name: "续写", exact:true }).click();
  await page.getByLabel("给 AI 的消息").fill("让主角接下不可退回的邮袋");
  await page.getByRole("button", { name: "发送消息" }).click();
  const continueAnyway = page.getByRole("button", { name: "仍然生成" });
  if (await continueAnyway.isVisible()) await continueAnyway.click();
  await expect(page.locator(".assistant-prose")).toContainText("夜班铃");
  await page.getByRole("button", { name: "写入正文",exact:true }).click();
  await expect(page.getByText("已写入正文，并保存为新版本。")).toBeVisible();
  await expectStudioReady(page, "雾城来信");

  await page.getByRole("button", { name: "资料库",exact:true }).click();
  await page.getByLabel("资料工作区").selectOption("assets");
  await page.getByLabel("素材类型").selectOption("scene");
  await page.getByLabel("画面要求").fill("雾潮中的第七码头，冷色电影感");
  await page.getByRole("button", { name: "生成概念图" }).click();
  await expect(page.getByRole("img", { name: "雾潮中的第七码头，冷色电影感" })).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "导出备份" }).click();
  await expect((await download).suggestedFilename()).toMatch(/\.zip$/);

  await page.getByRole("complementary", { name: "小说导航" })
    .getByRole("button", { name: "第一章：无主邮袋", exact: true }).click();
  await page.screenshot({ path: "test-results/final-studio.png", fullPage: true });
});
