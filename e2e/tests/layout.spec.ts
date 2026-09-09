import { expect, test } from '@playwright/test';

test('collapsing navigation expands the workspace and preserves the chat draft across screen sizes', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', { data: { title: '布局回归小说' } })).json();
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  const draft = page.getByRole('textbox', { name: '给 AI 的消息' });
  await draft.fill('保留这段尚未发送的创作想法');
  const workspace = page.getByRole('main', { name: '创作工作区' });
  const before = await workspace.boundingBox();
  await page.getByRole('button', { name: '收起侧栏', exact: true }).click();
  await expect(page.getByRole('button', { name: '展开侧栏' })).toHaveAttribute('aria-expanded', 'false');
  const after = await workspace.boundingBox();
  expect(after!.width).toBeGreaterThan(before!.width);
  await expect(draft).toBeInViewport();
  await expect(draft).toHaveValue('保留这段尚未发送的创作想法');
  await page.getByRole('button', { name: '打开章节目录' }).click();
  await expect(page.getByRole('dialog', { name: '章节目录' })).toBeVisible();
  await page.keyboard.press('Escape');

  await page.setViewportSize({ width: 390, height: 844 });
  for (const name of ['创作', '资料库', '设置']) {
    const button = page.getByRole('navigation', { name: '创作空间' }).getByRole('button', { name, exact: true });
    await expect(button.locator('span')).toBeVisible();
    await expect(button).toBeInViewport();
  }
  await expect(draft).toBeInViewport();
  await expect(draft).toHaveValue('保留这段尚未发送的创作想法');
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.getByRole('button', { name: '展开侧栏' }).click();
  await expect(page.getByRole('button', { name: '收起侧栏' })).toHaveAttribute('aria-expanded', 'true');
  await expect(draft).toHaveValue('保留这段尚未发送的创作想法');
});
