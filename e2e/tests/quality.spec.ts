import { expect, test } from '@playwright/test';

test('paired writing and quality handoffs preserve drafts until ordered acceptance and survive reload', async ({ page, request }) => {
  const projectResponse = await request.post('/api/v1/projects', { data: { title: '交替写作质量验收' } });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const base = `/api/v1/projects/${project.id}`;
  const chapters: Array<{ id: string; title: string }> = [];
  for (const [index, title] of ['第一章 · 无主来信', '第二章 · 钟楼回声'].entries()) {
    const response = await request.post(`${base}/nodes`, { data: { kind: 'chapter', title, order_index: index + 1 } });
    expect(response.status()).toBe(201);
    chapters.push(await response.json());
  }
  const originalVersions = await Promise.all(chapters.map(async chapter =>
    (await request.get(`${base}/chapters/${chapter.id}/versions`)).json()));

  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '质量优化', exact: true }).click();
  const workspace = page.getByRole('region', { name: '文章质量优化', exact: true });
  await workspace.getByRole('button', { name: '交替写作与优化', exact: true }).click();
  for (const chapter of chapters) await workspace.getByRole('checkbox', { name: chapter.title, exact: true }).check();
  await workspace.getByLabel('优化要求').fill('保留人物语气和悬念，衔接前章结尾，减少重复解释。');
  const submitted = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith(`${base}/quality/runs`));
  await workspace.getByRole('button', { name: '开始交替协作', exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  const created = await response.json();
  await expect(workspace.getByRole('heading', { name: '已完成', exact: true })).toBeVisible({ timeout: 20000 });
  await expect(workspace.getByText('已完成 2 / 2 章 · 优化稿由你确认后写入正文', { exact: true })).toBeVisible();

  const completed = await (await request.get(`${base}/quality/runs/${created.id}`)).json();
  expect(completed.status).toBe('succeeded');
  expect(completed.result.completed_chapters).toBe(2);
  expect(completed.result.messages.map((message: { role: string; chapter_id: string }) => [message.role, message.chapter_id]))
    .toEqual([['writer', chapters[0].id], ['optimizer', chapters[0].id], ['writer', chapters[1].id], ['optimizer', chapters[1].id]]);
  const writer = workspace.getByRole('region', { name: '写作会话', exact: true });
  const optimizer = workspace.getByRole('region', { name: '优化会话', exact: true });
  for (const [index, chapter] of chapters.entries()) {
    await expect(writer.getByText(`《${chapter.title}》已交稿，等待质量检测与优化。`, { exact: true })).toBeVisible();
    await expect(optimizer.getByText(chapter.title, { exact: true })).toBeVisible();
    await expect(workspace.getByRole('table', { name: `${chapter.title}质量评分`, exact: true })).toBeVisible();
    expect(completed.result.chapters[index].candidate_text.trim()).not.toBe('');
    const document = await (await request.get(`${base}/chapters/${chapter.id}`)).json();
    expect(document.content).toBe('');
    expect(await (await request.get(`${base}/chapters/${chapter.id}/versions`)).json()).toHaveLength(originalVersions[index].length);
  }

  // Later chapters use earlier candidates; the server must reject out-of-order acceptance.
  const outOfOrder = await request.post(`${base}/quality/runs/${created.id}/accept`, { data: { chapter_id: chapters[1].id } });
  expect(outOfOrder.status()).toBe(409);
  expect((await outOfOrder.json()).detail.code).toBe('QUALITY_ACCEPT_ORDER');
  for (const [index, chapter] of chapters.entries()) {
    await workspace.getByRole('button', { name: `采纳${chapter.title}的优化稿`, exact: true }).click();
    await expect(workspace.getByRole('button', { name: `${chapter.title}已采纳`, exact: true })).toBeDisabled();
    const document = await (await request.get(`${base}/chapters/${chapter.id}`)).json();
    expect(document.content).toBe(completed.result.chapters[index].candidate_text);
    const versions = await (await request.get(`${base}/chapters/${chapter.id}/versions`)).json();
    expect(versions).toHaveLength(originalVersions[index].length + 1);
    expect(versions.find((version: { id: string }) => version.id === document.current_version_id)?.content).toBe(document.content);
  }

  await page.reload();
  // Reopening restores the selected quality conversation directly.
  await expect(workspace.getByRole('heading', { name: '已完成', exact: true })).toBeVisible();
  for (const chapter of chapters) await expect(workspace.getByRole('button', { name: `${chapter.title}已采纳`, exact: true })).toBeDisabled();
  await workspace.getByText('查看优化后的正文', { exact: true }).first().click();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(workspace).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    expect(await workspace.evaluate(element => element.scrollWidth <= element.clientWidth)).toBeTruthy();
    // Trial clicks check scrolling and fixed mobile navigation without submitting another run.
    await workspace.getByRole('button', { name: '检测并优化正文', exact: true }).click({ trial: true });
    if (width !== 320) {
      await workspace.getByRole('heading', { name: '让故事更值得读下去', exact: true }).scrollIntoViewIfNeeded();
      await page.screenshot({ path: `../.verification/quality-${width}.png`, fullPage: true });
    }
  }
});

test('quality gate pauses the writer, resumes after approval and keeps cancelled candidates reviewable', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '质量未达标交接验收' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const chapters: Array<{ id: string; title: string }> = [];
  for (const [index, title] of ['第一章 · 雨夜交稿', '第二章 · 新的线索'].entries()) {
    const response = await request.post(`${base}/nodes`, { data: { kind: 'chapter', title, order_index: index + 1 } });
    expect(response.status()).toBe(201);
    chapters.push(await response.json());
  }
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '质量优化', exact: true }).click();
  const workspace = page.getByRole('region', { name: '文章质量优化', exact: true });
  await workspace.getByRole('button', { name: '交替写作与优化', exact: true }).click();
  for (const chapter of chapters) await workspace.getByRole('checkbox', { name: chapter.title, exact: true }).check();
  await workspace.getByText('质量目标与输入预算', { exact: true }).click();
  await workspace.getByLabel('质量目标', { exact: true }).fill('90');
  const submitted = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith(`${base}/quality/runs`));
  await workspace.getByRole('button', { name: '开始交替协作', exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  const created = await response.json();
  const readRun = async () => (await request.get(`${base}/quality/runs/${created.id}`)).json();
  await expect(workspace.getByText('本章需要你判断，写作会话已暂停', { exact: true })).toBeVisible({ timeout: 20000 });
  const paused = await readRun();
  expect(paused.status).toBe('recovery_required');
  expect(paused.error_code).toBe('QUALITY_REVIEW_REQUIRED');
  expect(paused.result.chapters).toHaveLength(1);
  expect(paused.result.chapters[0]).toMatchObject({ chapter_id: chapters[0].id, status: 'needs_review' });
  expect(paused.result.chapters[0].after.scores.readability).toBe(85);
  expect(paused.result.completed_chapters).toBe(0);
  expect(paused.result.messages.map((message: { role: string }) => message.role)).toEqual(['writer', 'optimizer']);
  await expect(workspace.getByRole('button', { name: `采纳${chapters[0].title}的优化稿`, exact: true })).toBeDisabled();
  // The second chapter must not begin until the first chapter's quality gate is approved.
  await expect(workspace.getByRole('table', { name: `${chapters[1].title}质量评分`, exact: true })).toHaveCount(0);
  await workspace.getByRole('button', { name: '认可此稿并继续', exact: true }).click();
  await expect.poll(async () => {
    const run = await readRun();
    return [run.status, run.result.active_chapter_id];
  }, { timeout: 20000 }).toEqual(['recovery_required', chapters[1].id]);
  await expect(workspace.getByRole('table', { name: `${chapters[1].title}质量评分`, exact: true })).toBeVisible();
  const resumed = await readRun();
  expect(resumed.result.completed_chapters).toBe(1);
  expect(resumed.result.chapters.map((chapter: { status: string }) => chapter.status)).toEqual(['ready', 'needs_review']);
  await workspace.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect(workspace.getByRole('heading', { name: '已取消', exact: true })).toBeVisible();
  for (const chapter of chapters) expect((await (await request.get(`${base}/chapters/${chapter.id}`)).json()).content).toBe('');

  await workspace.getByRole('button', { name: `采纳${chapters[0].title}的优化稿`, exact: true }).click();
  await expect(workspace.getByRole('button', { name: `${chapters[0].title}已采纳`, exact: true })).toBeDisabled();
  const confirmation = page.waitForEvent('dialog');
  const accepting = workspace.getByRole('button', { name: `采纳${chapters[1].title}的优化稿`, exact: true }).click();
  const dialog = await confirmation;
  expect(dialog.type()).toBe('confirm');
  expect(dialog.message()).toMatch(/质量|禁止揭示/);
  await dialog.accept();
  await accepting;
  await expect(workspace.getByRole('button', { name: `${chapters[1].title}已采纳`, exact: true })).toBeDisabled();
  for (const [index, chapter] of chapters.entries()) {
    expect((await (await request.get(`${base}/chapters/${chapter.id}`)).json()).content).toBe(resumed.result.chapters[index].candidate_text);
  }
});
