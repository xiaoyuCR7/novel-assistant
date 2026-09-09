import { expect, test } from "@playwright/test";

import { resetToEmptyWorkspace } from "./helpers/workspace";

test.beforeEach(async ({ request }) => {
  await resetToEmptyWorkspace(request);
});

test("创作准备：回答成为规则，返回准备不丢正文指令，仍可选择生成", async ({ page, request }) => {
  await page.goto("/");
  await page.getByLabel("小说名称").fill("群星回声");
  await page.getByLabel("核心前提").fill("失联舰队在百年后送回同一天的求救信号。");
  await page.getByLabel("类型").fill("硬科幻悬疑");
  await page.getByRole("button", { name: "新建空白小说" }).click();

  await expect(page.getByRole("heading", { name: "先固定会反复用到的规则" })).toBeVisible();
  await page.getByRole("button", { name: "开始整理" }).click();
  await page.getByRole("button", { name: "打开设定清单" }).click();
  await expect(page.getByRole("heading", { name: "高影响设定清单" })).toBeVisible();
  await page.getByLabel("作者回答").fill("任何超光速通信都会永久损失发送者的一段记忆。");
  await page.getByRole("button", { name: "保存回答" }).click();
  await expect(page.getByText(/已确认 1\/5/)).toBeVisible();

  const projects = await request.get("/api/v1/projects");
  const project = (await projects.json())[0];
  const volumeResponse = await request.post(`/api/v1/projects/${project.id}/nodes`, {
    data: { kind: "volume", title: "第一卷", order_index: 1 },
  });
  const volume = await volumeResponse.json();
  const chapterResponse = await request.post(`/api/v1/projects/${project.id}/nodes`, {
    data: {
      kind: "chapter",
      parent_id: volume.id,
      title: "第一章：回声",
      order_index: 1,
      target_words: 2000,
    },
  });
  const chapter = await chapterResponse.json();
  await page.reload();
  await page
    .getByRole("complementary", { name: "小说导航" })
    .getByRole("button", { name: chapter.title, exact: true })
    .click();
  await page.getByRole("button", { name: "续写", exact: true }).click();
  await page.getByLabel("给 AI 的消息").fill("从失真的求救信号切入");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByRole("dialog", { name: "正文生成前确认" })).toBeVisible();
  await page.getByRole("button", { name: "返回准备" }).click();
  await page.getByRole("button", { name: "创作", exact: true }).click();
  await expect(page.getByLabel("给 AI 的消息")).toHaveValue("从失真的求救信号切入");

  await page.getByRole("button", { name: "续写", exact: true }).click();
  await page.getByRole("button", { name: "发送消息" }).click();
  await page.getByRole("button", { name: "仍然生成" }).click();
  await expect(page.locator(".assistant-prose")).toContainText("夜班铃");
});

test("创作准备：延后问题保持未决并阻止过早追问", async ({ page, request }) => {
  await page.goto("/");
  await page.getByLabel("小说名称").fill("潮汐档案");
  await page.getByLabel("核心前提").fill("海水每天退去一小时，露出一段被删除的城市历史。");
  await page.getByLabel("类型").fill("都市奇幻悬疑");
  await page.getByRole("button", { name: "新建空白小说" }).click();
  await page.getByRole("button", { name: "开始整理" }).click();
  await page.getByRole("button", { name: "打开设定清单" }).click();

  await expect(page.getByRole("button", { name: "打开第 1 个问题，待回答" })).toBeVisible();
  await page.getByRole("button", { name: "稍后回答" }).click();
  await expect(page.getByRole("button", { name: "打开第 1 个问题，稍后回答" })).toBeVisible();

  const projects = await request.get("/api/v1/projects");
  const project = (await projects.json())[0];
  const preparation = await request.get(`/api/v1/projects/${project.id}/preparation`);
  expect(preparation.ok()).toBeTruthy();
  const body = await preparation.json();
  expect(body.status).toBe("in_progress");
  expect(body.unresolved_count).toBe(body.questions.length);
  expect(page.getByRole("button", { name: "进行一次追问检查" })).not.toBeVisible();
});

test("创作准备：离开并返回后重新发现进行中的智能任务", async ({ page, request }) => {
  await page.goto("/");
  await page.getByLabel("小说名称").fill("雾港来信");
  await page.getByLabel("核心前提").fill("每封寄往未来的信都会改写港口的一条街道。");
  await page.getByLabel("类型").fill("幻想悬疑");
  await page.getByRole("button", { name: "新建空白小说" }).click();
  await page.getByRole("button", { name: "开始整理" }).click();

  const projects = await request.get("/api/v1/projects");
  const project = (await projects.json())[0];
  const preparationResponse = await request.get(`/api/v1/projects/${project.id}/preparation`);
  const preparation = await preparationResponse.json();
  const jobId = "preparation-in-flight-e2e";
  let jobReads = 0;
  await page.route(`**/api/v1/projects/${project.id}/preparation`, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ...preparation, actionable_job_id: jobId }),
    });
  });
  await page.route(`**/api/v1/projects/${project.id}/ai/jobs/${jobId}`, async (route) => {
    jobReads += 1;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        id: jobId,
        project_id: project.id,
        task_type: "preparation_analysis",
        status: "running",
        context_snapshot: {},
        result: {},
        allowed_actions: ["cancel"],
      }),
    });
  });

  await page.reload();
  await page.getByRole("button", { name: "打开设定清单" }).click();
  await expect(page.getByText("智能补充正在后台分析，不影响继续回答。")).toBeVisible();
  await page.getByRole("button", { name: "资料库", exact: true }).click();
  await page.getByRole("button", { name: "创作", exact: true }).click();
  await page.getByRole("button", { name: "打开设定清单" }).click();
  await expect(page.getByText("智能补充正在后台分析，不影响继续回答。")).toBeVisible();
  expect(jobReads).toBeGreaterThan(0);
});
