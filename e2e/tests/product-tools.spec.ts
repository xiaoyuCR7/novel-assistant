import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { resetToEmptyWorkspace } from './helpers/workspace';

async function demoProject(request: APIRequestContext, title: string) {
  // Fail before submitting any generation if another test left a paid provider selected.
  const model = await (await request.get('/api/v1/settings/model')).json();
  expect(model.mode).toBe('demo');
  const response = await request.post('/api/v1/projects', { data: { title } });
  expect(response.status()).toBe(201);
  return response.json() as Promise<{ id: string; title: string }>;
}

async function responsive(page: Page, panel: Locator, name: string) {
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(panel).toBeVisible();
    await panel.evaluate(element => element.scrollIntoView({ block: 'start', behavior: 'instant' }));
    await expect(panel).toBeInViewport();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await expect.poll(() => panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: `../.verification/product-${name}-${width}.png`, fullPage: true, animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

test('unsaved manuscript recovers explicitly, todos open the saved chapter and quality target, and working export downloads exact text', async ({ page, request }) => {
  const project = await demoProject(request, '产品验收 · 恢复与交付');
  const base = `/api/v1/projects/${project.id}`;
  const created = await request.post(`${base}/nodes`, { data: { kind: 'chapter', title: '第一章 · 灯塔来信', order_index: 1 } });
  expect(created.status()).toBe(201);
  const chapter = await created.json();
  const chapterUrl = `${base}/chapters/${chapter.id}`;
  const initial = await (await request.get(chapterUrl)).json();
  const original = '灯塔亮起时，信还封着。';
  expect((await request.put(chapterUrl, { data: { content: original, contract: {}, revision: initial.revision } })).status()).toBe(200);
  const revised = '灯塔亮起时，信还封着。\n\n林渡把钥匙放在窗边，等下一阵潮声。';
  const purpose = '让林渡决定留下等待来信人';
  const forbidden = '信件署名';

  // Every subsequent UI action in this test is navigation, local editing or export.
  const generatedByUI: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && /\/(ai\/jobs|quality\/runs)$/.test(new URL(request.url()).pathname)) generatedByUI.push(request.url());
  });
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '查看正文', exact: true }).click();
  await page.getByLabel('章节正文').fill(revised);
  await page.getByLabel('本章目的').fill(purpose);
  await page.getByLabel('禁止提前揭示').fill(forbidden);
  await expect(page.getByText('草稿已暂存本机 · 尚未保存到项目', { exact: true })).toBeVisible();
  expect((await (await request.get(chapterUrl)).json()).content).toBe(original);
  // A reload may raise the application's standard beforeunload warning.
  const acceptUnload = (dialog: import('@playwright/test').Dialog) => { void dialog.accept(); };
  page.on('dialog', acceptUnload);
  try { await page.reload(); } finally { page.off('dialog', acceptUnload); }
  // Only navigation IDs persist; the manuscript drawer must be opened explicitly again.
  await page.getByRole('button', { name: '查看正文', exact: true }).click();
  await expect(page.getByLabel('章节正文')).toHaveValue(original);
  await expect(page.getByRole('button', { name: '恢复章节草稿', exact: true })).toBeVisible();
  await responsive(page, page.locator('.chapter-workspace'), 'draft-recovery');
  await page.getByRole('button', { name: '恢复章节草稿', exact: true }).click();
  await expect(page.getByLabel('章节正文')).toHaveValue(revised);
  await expect(page.getByLabel('本章目的')).toHaveValue(purpose);
  await expect(page.getByLabel('禁止提前揭示')).toHaveValue(forbidden);
  expect((await (await request.get(chapterUrl)).json()).content).toBe(original);
  await page.getByRole('button', { name: '保存工作副本', exact: true }).click();
  await expect(page.getByText('工作副本已保存', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '保存工作副本', exact: true })).toBeEnabled();
  const saved = await (await request.get(chapterUrl)).json();
  expect(saved.content).toBe(revised);
  expect(saved.contract).toMatchObject({ purpose, forbidden_revelations: [forbidden] });
  await page.getByRole('button', { name: '收起正文', exact: true }).click();

  // Prepare an unaccepted, free Demo quality candidate through the real isolated API.
  const qualityResponse = await request.post(`${base}/quality/runs`, {
    headers: { 'Idempotency-Key': randomUUID() },
    data: { mode: 'polish', chapters: [{ chapter_id: chapter.id, expected_revision: saved.revision }], quality_target: 75 },
  });
  expect(qualityResponse.status()).toBe(202);
  const quality = await qualityResponse.json();
  await expect.poll(async () => (await (await request.get(`${base}/quality/runs/${quality.id}`)).json()).status,
    { timeout: 20000 }).toBe('succeeded');

  await page.getByRole('button', { name: '创作待办', exact: true }).click();
  let todos = page.getByRole('dialog', { name: '创作待办', exact: true });
  await expect(todos.getByRole('button', { name: '查看优化稿', exact: true })).toBeVisible();
  await responsive(page, todos, 'todos');
  await todos.getByRole('button', { name: '打开章节', exact: true }).click();
  await expect(todos).not.toBeVisible();
  await expect(page.getByLabel('章节正文')).toHaveValue(revised);
  await page.getByRole('button', { name: '创作待办', exact: true }).click();
  todos = page.getByRole('dialog', { name: '创作待办', exact: true });
  await todos.getByRole('button', { name: '查看优化稿', exact: true }).click();
  const workspace = page.getByRole('region', { name: '文章质量优化', exact: true });
  await expect(workspace.getByRole('heading', { name: '已完成', exact: true })).toBeVisible();
  await expect(workspace.getByRole('table', { name: `${chapter.title}质量评分`, exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(id => JSON.parse(localStorage.getItem(`studio:workspace:${id}`) ?? '{}').qualityRunId, project.id)).toBe(quality.id);
  expect((await (await request.get(chapterUrl)).json()).content).toBe(revised);

  await page.getByRole('button', { name: '导出正文', exact: true }).click();
  const exporter = page.getByRole('dialog', { name: '导出小说正文', exact: true });
  await exporter.getByRole('combobox', { name: '正文来源', exact: true }).selectOption('working');
  await responsive(page, exporter, 'export');
  const downloading = page.waitForEvent('download');
  await exporter.getByRole('button', { name: '下载正文', exact: true }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe(`manuscript-${project.id}.txt`);
  expect(await download.failure()).toBeNull();
  const downloadedPath = await download.path();
  expect(downloadedPath).not.toBeNull();
  expect(await readFile(downloadedPath!, 'utf8')).toBe(`${project.title}\n\n${chapter.title}\n\n${revised}\n`);
  expect(generatedByUI).toEqual([]);
});

test('new conversations and branches restore their own history, with usable conversation and spending panels at narrow widths', async ({ page, request }) => {
  const project = await demoProject(request, '产品验收 · 独立方案会话');
  const base = `/api/v1/projects/${project.id}`;
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.locator('details.conversation-management > summary').click();
  const conversations = page.getByRole('region', { name: '会话管理', exact: true });
  await conversations.getByRole('button', { name: '新建会话', exact: true }).click();
  await conversations.getByLabel('会话名称', { exact: true }).fill('灯塔来信方案');
  const creating = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith(`${base}/conversations`));
  await conversations.getByRole('button', { name: '创建会话', exact: true }).click();
  const created = await creating;
  expect(created.status()).toBe(201);
  const thread = await created.json();
  await expect(conversations.getByText('当前：灯塔来信方案', { exact: true })).toBeVisible();

  async function discuss(instructions: string, conversationId: string) {
    await page.getByRole('button', { name: '讨论', exact: true }).click();
    await page.getByLabel('给 AI 的消息').fill(instructions);
    const sending = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith(`${base}/ai/jobs`));
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
    const response = await sending;
    expect(response.status()).toBe(202);
    expect(response.request().postDataJSON()).toMatchObject({ conversation_id: conversationId, instructions, task_type: 'chat' });
    const job = await response.json();
    await expect.poll(async () => (await (await request.get(`${base}/ai/jobs/${job.id}?conversation_id=${conversationId}`)).json()).status,
      { timeout: 20000 }).toBe('succeeded');
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled();
    return job;
  }
  const firstPrompt = '只讨论灯塔来信的开场悬念，不生成正文。';
  const first = await discuss(firstPrompt, thread.id);
  await expect(page.locator('.assistant-prose')).toContainText('离线演示');
  await expect(conversations.getByRole('button', { name: '从选中消息分支', exact: true })).toBeEnabled();
  await conversations.getByRole('button', { name: '从选中消息分支', exact: true }).click();
  await conversations.getByLabel('会话名称', { exact: true }).fill('来信人身份分支');
  const branching = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith(`${base}/conversations/${thread.id}/branches`));
  await conversations.getByRole('button', { name: '创建分支', exact: true }).click();
  const branchResponse = await branching;
  expect(branchResponse.status()).toBe(201);
  const branch = await branchResponse.json();
  expect(branch).toMatchObject({ parent_conversation_id: thread.id, branch_from_job_id: first.id, chapter_id: null });
  await expect(page.getByText('分支继承记录 · 只读', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator('.author-message')).toContainText(firstPrompt);
  await expect(page.getByText('分支继承记录 · 只读', { exact: true })).toBeVisible();
  const secondPrompt = '这个分支让来信人保持匿名，继续讨论。';
  const second = await discuss(secondPrompt, branch.id);
  await page.reload();
  await expect(page.locator('.author-message > p')).toHaveText([firstPrompt, secondPrompt]);
  const originalHistory = await (await request.get(`${base}/ai/jobs/page?conversation_id=${thread.id}`)).json();
  const branchHistory = await (await request.get(`${base}/ai/jobs/page?conversation_id=${branch.id}`)).json();
  const defaultHistory = await (await request.get(`${base}/ai/jobs/page`)).json();
  expect(originalHistory.items.map((item: { id: string }) => item.id)).toEqual([first.id]);
  expect(branchHistory.items.map((item: { id: string }) => item.id).sort()).toEqual([first.id, second.id].sort());
  expect(branchHistory.items.find((item: { id: string }) => item.id === first.id).inherited).toBe(true);
  expect(defaultHistory.items).toEqual([]);
  await page.locator('details.conversation-management > summary').click();
  await expect(conversations.getByText('当前：来信人身份分支（从选定消息分支）', { exact: true })).toBeVisible();
  await responsive(page, conversations, 'conversations');

  // Inspect the free Demo ledger without mutating settings shared by other specs.
  await page.getByRole('button', { name: '设置', exact: true }).click();
  const spending = page.getByRole('region', { name: '费用与预算', exact: true });
  await expect(spending.getByText('暂无付费调用记录。历史版本升级前的请求不会回填为零元。', { exact: true })).toHaveCount(1);
  const report = await (await request.get(`/api/v1/settings/spending?project_id=${project.id}`)).json();
  expect(Number(report.totals.settled_cny)).toBe(0);
  expect(Number(report.totals.reserved_cny)).toBe(0);
  await responsive(page, spending, 'spending');
});

test('backup preview prevents overwrite and restores a saved project with its history through the empty workspace', async ({ page, request }) => {
  const project = await demoProject(request, '产品验收 · 项目灾备');
  const base = `/api/v1/projects/${project.id}`;
  const chapterResponse = await request.post(`${base}/nodes`, { data: { kind: 'chapter', title: '第一章 · 归航', order_index: 1 } });
  expect(chapterResponse.status()).toBe(201);
  const chapter = await chapterResponse.json();
  const chapterUrl = `${base}/chapters/${chapter.id}`;
  const original = await (await request.get(chapterUrl)).json();
  const content = '归航时，灯塔依旧亮着。';
  expect((await request.put(chapterUrl, { data: { content, contract: {}, revision: original.revision } })).status()).toBe(200);
  const submitted = await request.post(`${base}/ai/jobs`, {
    headers: { 'Idempotency-Key': randomUUID() },
    data: { project_id: project.id, task_type: 'chat', instructions: '只讨论归航意象，不写正文。' },
  });
  expect(submitted.status()).toBe(202);
  const job = await submitted.json();
  await expect.poll(async () => (await (await request.get(`${base}/ai/jobs/${job.id}`)).json()).status).toBe('succeeded');
  const archive = await request.get(`${base}/export`);
  expect(archive.status()).toBe(200);
  const file = { name: 'project-backup.zip', mimeType: 'application/zip', buffer: await archive.body() };
  const generatedByUI: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && /\/(ai\/jobs|quality\/runs)$/.test(new URL(request.url()).pathname)) generatedByUI.push(request.url());
  });
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '恢复项目备份', exact: true }).click();
  const restore = page.getByRole('region', { name: '恢复项目备份', exact: true });
  await restore.getByLabel('项目备份 ZIP', { exact: true }).setInputFiles(file);
  await restore.getByRole('button', { name: '检查备份', exact: true }).click();
  await expect(restore.getByText('同一项目已存在，无法覆盖。请在空白工作区中恢复这份备份。', { exact: true })).toBeVisible();
  await expect(restore.getByRole('button', { name: '恢复项目', exact: true })).toBeDisabled();
  await restore.getByRole('button', { name: '关闭', exact: true }).click();

  // The reset endpoint exists only in the guarded, isolated E2E app.
  await resetToEmptyWorkspace(request);
  await page.reload();
  await page.getByRole('button', { name: '从备份恢复小说', exact: true }).click();
  await restore.getByLabel('项目备份 ZIP', { exact: true }).setInputFiles(file);
  await restore.getByRole('button', { name: '检查备份', exact: true }).click();
  await expect(restore.getByText('文件完整性校验通过', { exact: true })).toBeVisible();
  await responsive(page, restore, 'backup-restore');
  await restore.getByRole('checkbox').check();
  const restoring = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/projects/backup/restore'));
  await restore.getByRole('button', { name: '恢复项目', exact: true }).click();
  expect((await restoring).status()).toBe(201);
  await expect(restore).toHaveCount(0);
  await expect(page.getByLabel('当前小说项目')).toHaveValue(project.id);
  expect((await (await request.get(chapterUrl)).json()).content).toBe(content);
  const restoredHistory = await (await request.get(`${base}/ai/jobs/${job.id}`)).json();
  expect(restoredHistory).toMatchObject({ id: job.id, status: 'succeeded', instructions: '只讨论归航意象，不写正文。' });
  await page.getByRole('button', { name: '全书讨论', exact: true }).click();
  await expect(page.locator('.author-message')).toContainText('只讨论归航意象，不写正文。');
  expect(generatedByUI).toEqual([]);
});
