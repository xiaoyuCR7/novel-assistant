import { expect, test, type Locator, type Page } from '@playwright/test';

async function responsive(page: Page, panel: Locator, name: string) {
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await panel.evaluate(element => element.scrollIntoView({ block: 'start', behavior: 'instant' }));
    await expect(panel).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: `../.verification/followthrough-${name}-${width}.png`, animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

test('goals persist and coverage opens a fresh review without submitting generation', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '后续验收 · 目标与覆盖' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const chapter = await (await request.post(`${base}/nodes`, { data: { kind: 'chapter', title: '灯火归来', order_index: 1 } })).json();
  const source = await (await request.get(`${base}/chapters/${chapter.id}`)).json();
  expect((await request.put(`${base}/chapters/${chapter.id}`, { data: { content: '天亮前，灯火回到了港口。', contract: {}, revision: source.revision } })).status()).toBe(200);
  const generation: string[] = [];
  page.on('request', request => { if (request.method() === 'POST' && /\/(ai\/jobs|quality\/runs)$/.test(new URL(request.url()).pathname)) generation.push(request.url()); });
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '创作目标', exact: true }).click();
  const goals = page.getByRole('region', { name: '创作与交稿目标', exact: true });
  await goals.getByLabel('全书目标字数', { exact: true }).fill('160000');
  await goals.getByLabel('每周目标章节数', { exact: true }).fill('3');
  await goals.getByLabel('截止日期（可选）', { exact: true }).fill('2026-12-31');
  await goals.getByRole('button', { name: '保存创作目标', exact: true }).click();
  await expect(goals.getByText('创作目标已保存。', { exact: true })).toBeVisible();
  await responsive(page, goals, 'goals');
  await page.reload();
  await page.getByRole('button', { name: '创作目标', exact: true }).click();
  await expect(goals.getByLabel('全书目标字数', { exact: true })).toHaveValue('160000');
  await expect(goals.getByLabel('每周目标章节数', { exact: true })).toHaveValue('3');
  await expect(goals.getByLabel('截止日期（可选）', { exact: true })).toHaveValue('2026-12-31');
  await page.getByRole('dialog').getByRole('button', { name: '关闭对话框', exact: true }).click();
  await page.getByRole('button', { name: '整书审校', exact: true }).click();
  const coverage = page.getByRole('region', { name: '整书审校覆盖', exact: true });
  await expect(coverage.getByRole('heading', { name: '灯火归来' })).toBeVisible();
  await responsive(page, coverage, 'coverage');
  await coverage.getByRole('button', { name: '复核本章', exact: true }).click();
  await expect(page.getByRole('button', { name: '检测并优化正文', exact: true })).toBeVisible();
  await expect(page.locator('.quality-field select').first()).toHaveValue(chapter.id);
  expect(generation).toEqual([]);
});

test('named models switch saved settings and automatic backups download a valid isolated project', async ({ page, request }) => {
  const original = await (await request.get('/api/v1/settings/model')).json();
  expect(original.mode).toBe('demo');
  const project = await (await request.post('/api/v1/projects', { data: { title: '后续验收 · 方案与备份' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  try {
    await page.goto('/'); await page.getByLabel('当前小说项目').selectOption(project.id);
    await page.getByRole('button', { name: '设置', exact: true }).click();
    const profiles = page.getByRole('region', { name: '命名模型方案', exact: true });
    await profiles.getByRole('button', { name: '保存为命名方案', exact: true }).click();
    await profiles.getByLabel('方案名称', { exact: true }).fill('离线审校方案');
    await profiles.getByRole('button', { name: '保存方案', exact: true }).click();
    await expect(profiles.getByRole('button', { name: '应用方案 离线审校方案', exact: true })).toBeDisabled();
    await page.getByText('生成限制与兼容性', { exact: true }).click();
    await page.getByLabel('模型上下文容量', { exact: true }).fill(String(original.context_capacity === 65536 ? 32768 : 65536));
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    const apply = profiles.getByRole('button', { name: '应用方案 离线审校方案', exact: true });
    await expect(apply).toBeEnabled(); await apply.click();
    await expect.poll(async () => (await (await request.get('/api/v1/settings/model')).json()).context_capacity).toBe(original.context_capacity);
    await expect(profiles.getByText('当前配置', { exact: true })).toBeVisible();
    await expect(apply).toBeDisabled();
    await responsive(page, profiles, 'profiles');
    const backups = page.getByRole('region', { name: '自动备份', exact: true });
    await backups.getByRole('button', { name: '立即备份', exact: true }).click();
    await expect(backups.getByText('备份已创建并通过完整性校验。', { exact: true })).toBeVisible();
    await backups.getByText('查看与下载备份', { exact: true }).click();
    const link = backups.getByRole('link', { name: '下载 ZIP', exact: true });
    const archive = await request.get((await link.getAttribute('href'))!);
    expect(archive.status()).toBe(200);
    const preview = await request.post('/api/v1/projects/backup/preview', { multipart: { file: { name: 'snapshot.zip', mimeType: 'application/zip', buffer: await archive.body() } } });
    expect((await preview.json())).toMatchObject({ project_id: project.id, verified: true, conflict: true });
    await responsive(page, backups, 'backups');
    await backups.getByRole('button', { name: '立即备份', exact: true }).click();
    await expect(backups.getByText('项目内容未变化，已保留最近的有效备份。', { exact: true })).toBeVisible();
    expect((await (await request.get(`${base}/automatic-backups`)).json()).backups).toHaveLength(1);
  } finally {
    const current = await (await request.get('/api/v1/settings/model')).json();
    const fields = ['mode', 'base_url', 'model', 'external_consent', 'output_token_budget', 'context_capacity', 'deadline_seconds', 'output_parameter', 'thinking_mode'];
    const restore = Object.fromEntries(fields.map(key => [key, original[key]]));
    expect((await request.put('/api/v1/settings/model', { data: { ...restore, expected_config_revision: current.config_revision } })).status()).toBe(200);
  }
});
