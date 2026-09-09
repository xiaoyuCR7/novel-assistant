import { expect, test } from '@playwright/test';

test('durable writing and summary survive reload without automatic acceptance', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '持久任务验收' } })).json();
  const base = `/api/v1/projects/${project.id}`;
  const chapter = await (await request.post(base + '/nodes', { data: { kind: 'chapter', title: '第一章' } })).json();
  const original = await (await request.get(base + '/chapters/' + chapter.id)).json();
  expect((await request.put(base + '/chapters/' + chapter.id, {
    data: { content: '作者保存的开头。', revision: original.revision },
  })).status()).toBe(200);
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '续写', exact: true }).click();
  await page.getByLabel('给 AI 的消息').fill('继续写下去');
  const submitted = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/ai/jobs'));
  await page.getByRole('button', { name: '发送消息' }).click();
  await page.getByRole('button', { name: '仍然生成' }).click();
  expect((await submitted).status()).toBe(202);
  await page.reload();
  await expect(page.getByRole('button', { name: '写入正文', exact: true })).toBeVisible();
  expect((await (await request.get(base + '/chapters/' + chapter.id)).json()).content).toBe('作者保存的开头。');
  await page.getByRole('button', { name: '写入正文', exact: true }).click();
  await expect(page.getByText('已写入正文，并保存为新版本。')).toBeVisible();
  const completing = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/complete'));
  await page.getByRole('button', { name: '完成本章', exact: true }).click();
  expect((await completing).status()).toBe(202);
  await page.reload();
  await page.getByRole('button', { name: '查看正文' }).click();
  await page.getByText('章节总结与连续性', { exact: true }).click();
  await expect(page.locator('.summary-panel').getByText('AI 总结', { exact: true })).toBeVisible();
  const tasks = await (await request.get(base + '/ai/jobs?kind=summary&chapter_id=' + chapter.id)).json();
  expect(tasks).toHaveLength(1);
  expect(tasks[0].effects.ledger_pending).toBe(false);
});
