import { expect, test } from '@playwright/test';

test('Wiki searches aliases, cites sources, caches synthesis, invalidates edits and fits mobile', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: 'Wiki 验证小说' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const entity = await (await request.post(`${base}/library/entity`, { data: { title: '林渡', content: '旧城邮差', kind: 'character' } })).json();
  const patch = await request.patch(`${base}/library/entity/${entity.id}`, { data: {
    revision: entity.revision, fields: { profile: { aliases: ['灰鸦'], occupation: '邮差' } },
  } });
  expect(patch.ok()).toBeTruthy();
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await page.getByRole('button', { name: '打开小说 Wiki', exact: true }).click();
  await page.getByLabel('搜索 Wiki 条目').fill('灰鸦');
  const article = page.getByRole('article', { name: 'Wiki 条目详情' });
  await expect(article.getByRole('heading', { name: '林渡', exact: true, level: 2 })).toBeVisible();
  await article.getByRole('button', { name: 'AI摘要', exact: true }).click();
  await expect(article.getByText(/离线演示摘录/)).toBeVisible({ timeout: 15000 });
  const before = await (await request.get(`${base}/wiki/entity/${entity.id}`)).json();
  const repeat = await request.post(`${base}/wiki/entity/${entity.id}/summarize`, { headers: { 'Idempotency-Key': 'wiki-cache-e2e' }, data: { fingerprint: before.fingerprint } });
  expect((await repeat.json()).id).toBe(before.summary.job_id);
  await article.getByRole('button', { name: '打开原始资料：林渡' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  const current = await (await request.get(`${base}/library/entity/${entity.id}`)).json();
  expect((await request.patch(`${base}/library/entity/${entity.id}`, { data: { revision: current.revision, content: '已确认的新设定：北城邮差' } })).ok()).toBeTruthy();
  await page.reload();
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await page.getByRole('button', { name: '打开小说 Wiki', exact: true }).click();
  await expect(article.getByText(/来源.*变化|摘要.*过期/).first()).toBeVisible();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(article).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await article.scrollIntoViewIfNeeded();
    if (width !== 320) await page.screenshot({ path: `../.verification/wiki-${width}.png`, fullPage: true });
  }
});
