import { expect, test, type APIRequestContext } from '@playwright/test';

async function completedJob(request: APIRequestContext, url: string) {
  await expect.poll(async () => (await (await request.get(url)).json()).status,
    { timeout: 20000 }).toBe('succeeded');
  return (await request.get(url)).json();
}

test('reload restores a chapter conversation and the complete selected older message', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '会话位置恢复验收' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const chapters = [];
  for (const [index, title] of ['第一章 · 初见', '第二章 · 重逢'].entries()) {
    chapters.push(await (await request.post(`${base}/nodes`, {
      data: { kind: 'chapter', title, order_index: index + 1 },
    })).json());
  }
  const chapter = chapters[1];
  const document = await (await request.get(`${base}/chapters/${chapter.id}`)).json();
  const fullMessage = '请保留人物的克制语气。'.repeat(110) + '原消息结尾：不要提前揭示寄信人。';
  const created = [];
  for (const [index, instructions] of [fullMessage, '再讨论下一场景的情绪变化。'].entries()) {
    const response = await request.post(`${base}/ai/jobs`, {
      headers: { 'Idempotency-Key': `conversation-${index}` },
      data: { project_id: project.id, chapter_id: chapter.id, expected_revision: document.revision,
        task_type: 'chat', instructions },
    });
    expect(response.status()).toBe(202);
    const receipt = await response.json();
    created.push(await completedJob(request, receipt.status_url));
  }
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: chapter.title, exact: true }).click();
  await page.getByRole('button', { name: `查看完整回复 ${created[0].id}`, exact: true }).click();
  await expect(page.locator('.author-message').first()).toContainText(fullMessage);
  await page.reload();
  await expect(page.getByRole('heading', { name: chapter.title, exact: true })).toBeVisible();
  await expect(page.locator('.author-message').first()).toContainText(fullMessage);
  await expect(page.locator('.assistant-prose').first()).toHaveText(created[0].result.reply);

  await page.getByRole('button', { name: '全书讨论', exact: true }).click();
  await page.reload();
  await expect(page.getByRole('heading', { name: '聊聊这个故事', exact: true })).toBeVisible();
  await expect(page.locator('.author-message')).toHaveCount(0);
  await page.getByRole('button', { name: chapter.title, exact: true }).click();
  await expect(page.locator('.author-message').first()).toContainText(fullMessage);
});

test('reload returns directly to the selected older quality conversation', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '质量会话恢复验收' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const chapters = [];
  for (const [index, title] of ['第一章 · 昨日交接', '第二章 · 今日交接'].entries()) {
    const chapter = await (await request.post(`${base}/nodes`, {
      data: { kind: 'chapter', title, order_index: index + 1 },
    })).json();
    chapters.push(chapter);
    const document = await (await request.get(`${base}/chapters/${chapter.id}`)).json();
    const response = await request.post(`${base}/quality/runs`, {
      headers: { 'Idempotency-Key': `quality-memory-${index}` },
      data: { mode: 'collaborate', chapters: [{ chapter_id: chapter.id, expected_revision: document.revision }],
        quality_target: 75, token_budget: 12000, instructions: '保留悬念。' },
    });
    expect(response.status()).toBe(202);
    const receipt = await response.json();
    await completedJob(request, `${base}/quality/runs/${receipt.id}`);
  }
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '质量优化', exact: true }).click();
  const workspace = page.getByRole('region', { name: '文章质量优化', exact: true });
  const records = workspace.getByRole('navigation', { name: '质量优化记录' });
  await expect(records.getByRole('button')).toHaveCount(2);
  await records.getByRole('button').last().click();
  await expect(workspace.getByRole('table', { name: `${chapters[0].title}质量评分`, exact: true })).toBeVisible();
  await page.reload();
  await expect(workspace).toBeVisible();
  await expect(workspace.getByRole('table', { name: `${chapters[0].title}质量评分`, exact: true })).toBeVisible();
  await expect(records.getByRole('button').last()).toHaveAttribute('aria-current', 'page');
  await expect(workspace.getByRole('region', { name: '写作会话', exact: true })).toContainText(chapters[0].title);
  await expect(workspace.getByRole('region', { name: '优化会话', exact: true })).toContainText(chapters[0].title);
});
