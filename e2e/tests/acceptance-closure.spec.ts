import { expect, test } from '@playwright/test';

test('ordinary chat bootstraps without full library, workspace or visual assets', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '轻量启动验收' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const paths: string[] = [];
  page.on('request', incoming => {
    const url = new URL(incoming.url());
    if (incoming.method() === 'GET' && url.pathname.startsWith(base)) paths.push(url.pathname);
  });
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await expect(page.getByLabel('给 AI 的消息')).toBeVisible();
  await expect.poll(() => paths.includes(base + '/workspace/navigation')).toBe(true);
  expect(paths).not.toContain(base + '/workspace');
  expect(paths).not.toContain(base + '/library');
  expect(paths).not.toContain(base + '/assets');
});

test('editing a search hit preserves the complete material, not just its chunk', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '全文编辑验收' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const original = 'Original opening. '.repeat(400) + 'copperkey is hidden here. ' + 'Original ending. '.repeat(600);
  const created = await request.post(base + '/library/idea', {
    data: { title: '长篇设定档案', content: original },
  });
  expect(created.status()).toBe(201);
  const material = await created.json();
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '参考资料', exact: true }).click();
  const searched = page.waitForResponse(response => response.url().includes('/library/search?')
    && response.url().includes('copperkey'));
  await page.getByLabel('搜索本项目素材').fill('copperkey');
  expect((await searched).status()).toBe(200);
  await page.locator('.material-row').filter({ hasText: '长篇设定档案' }).click();
  await page.getByRole('button', { name: '编辑素材', exact: true }).click();
  const content = page.getByLabel('设定内容');
  await expect(content).toHaveValue(original);
  await content.fill(original + '\nAuthor-approved addition.');
  await page.getByRole('button', { name: '保存素材', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '编辑素材' })).not.toBeVisible();
  // Legacy full endpoint remains a compatibility oracle independent of the new detail path.
  const all = await (await request.get(base + '/library')).json();
  expect(all.find((item: { id: string }) => item.id === material.id).content)
    .toBe(original + '\nAuthor-approved addition.');
});

test('normalized chapter saves permit chat and an explicit budget unblocks a long chapter', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '长章节预算验收' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const chapter = await (await request.post(base + '/nodes', {
    data: { kind: 'chapter', title: '第一章' },
  })).json();
  const original = '风'.repeat(7000);
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '查看正文', exact: true }).click();
  await page.getByLabel('章节正文').fill(original);
  await page.getByLabel('禁止提前揭示').fill('secret A, secret B，secret C');
  await page.getByRole('button', { name: '保存工作副本' }).click();
  await expect(page.getByText('工作副本已保存')).toBeVisible();
  // Persistence notice precedes post-save query refresh; wait for the save operation to settle.
  await expect(page.getByRole('button', { name: '保存工作副本', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '收起正文', exact: true }).click();
  await expect(page.getByLabel('章节正文')).toHaveCount(0);
  await page.getByRole('button', { name: '续写', exact: true }).click();
  const prompt = page.getByLabel('给 AI 的消息');
  await prompt.fill('继续写一段风中的故事');
  const preflight = page.waitForResponse(response => response.url().endsWith('/ai/jobs/preflight'));
  await page.getByRole('button', { name: '发送消息' }).click();
  await page.getByRole('button', { name: '仍然生成' }).click();
  const report = await (await preflight).json();
  expect(report.can_fit).toBe(false);
  expect(report.token_budget).toBe(12000);
  await expect(page.getByRole('alert')).toContainText('当前输入预算 12000');
  await expect(prompt).toHaveValue('继续写一段风中的故事');
  expect(await (await request.get(base + '/ai/jobs')).json()).toHaveLength(0);
  await page.getByLabel('输入预算 Token').fill('20000');
  const submitted = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith('/ai/jobs'));
  await page.getByRole('button', { name: '发送消息' }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  expect(response.request().postDataJSON().token_budget).toBe(20000);
  await expect(page.getByRole('button', { name: '写入正文', exact: true })).toBeVisible();
  const document = await (await request.get(base + '/chapters/' + chapter.id)).json();
  expect(document.content).toBe(original);
  expect(document.contract.forbidden_revelations).toEqual(['secret A', 'secret B', 'secret C']);
});
