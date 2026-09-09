import { expect, test } from '@playwright/test';

test('a chapter deleted elsewhere does not discard the open unsaved manuscript', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '失效章节草稿保护' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const chapter = await (await request.post(base + '/nodes', { data: { kind: 'chapter', title: '章节 A', order_index: 1 } })).json();
  await request.post(base + '/nodes', { data: { kind: 'chapter', title: '章节 B', order_index: 2 } });
  await request.post(base + '/library/idea', { data: { title: '用于刷新导航的资料', content: '本地测试素材' } });
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '查看正文', exact: true }).click();
  const draft = '只有这个编辑器中存在的未保存正文。';
  await page.getByLabel('章节正文').fill(draft);
  await page.getByLabel('禁止提前揭示').fill('保留的禁揭示草稿');
  await page.getByRole('button', { name: '参考资料', exact: true }).click();
  await page.locator('.material-row').filter({ hasText: '用于刷新导航的资料' }).click();
  expect((await request.delete(base + '/library/node/' + chapter.id)).status()).toBe(200);
  // A normal material mutation refreshes navigation, as does another tab's update.
  const refreshed = page.waitForResponse(response => response.url().endsWith(base + '/workspace/navigation'));
  await page.getByRole('button', { name: '固定素材', exact: true }).click();
  expect((await refreshed).status()).toBe(200);
  await page.getByRole('dialog', { name: '素材详情' }).getByRole('button', { name: '关闭对话框' }).click();
  await expect(page.getByLabel('章节正文')).toHaveValue(draft);
  await expect(page.getByLabel('禁止提前揭示')).toHaveValue('保留的禁揭示草稿');
  await expect(page.getByRole('button', { name: '保存工作副本', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '完成本章', exact: true })).toBeDisabled();
});

test('late idea input survives the successful response for the earlier submission', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '资料提交输入保护' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**' + base + '/ideas', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await page.getByLabel('资料工作区').selectOption('ideas');
  await page.getByLabel('灵感标题').fill('第一次提交');
  await page.getByLabel('灵感内容').fill('提交的内容');
  const sent = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith(base + '/ideas'));
  await page.getByRole('button', { name: '收进灵感匣', exact: true }).click();
  await sent;
  try {
    await expect(page.getByRole('button', { name: '收进灵感匣', exact: true })).toBeDisabled();
    await page.getByLabel('灵感内容').fill('提交后继续输入，必须保留。');
  } catch (error) {
    release();
    throw error;
  }
  const received = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith(base + '/ideas'));
  release();
  expect((await received).status()).toBe(201);
  await expect(page.getByRole('button', { name: '收进灵感匣', exact: true })).toBeEnabled();
  await expect(page.getByLabel('灵感内容')).toHaveValue('提交后继续输入，必须保留。');
  const view = await (await request.get(base + '/workspace/views/ideas')).json();
  expect(view.ideas).toHaveLength(1);
  expect(view.ideas[0].content).toBe('提交的内容');
});
