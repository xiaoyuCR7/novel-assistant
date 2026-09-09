import { expect, test } from '@playwright/test';

test('an archived correction becomes effective only after an explicit author preference', async ({ page, request }) => {
  const project = await (await request.post('/api/v1/projects', {
    data: { title: '反馈偏好闭环验收' },
  })).json();
  const base = `/api/v1/projects/${project.id}`;
  const created = await (await request.post(base + '/feedback', {
    data: { project_id: project.id, rating: 3, original_text: '原句', corrected_text: '修正句' },
  })).json();
  const candidate = created.preference_candidate;
  expect((await request.post(base + `/feedback/preferences/${candidate.id}/confirm`)).status()).toBe(422);
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await page.getByLabel('资料工作区').selectOption('style');
  const confirm = page.getByRole('button', { name: '确认风格规则', exact: true });
  await expect(confirm).toBeDisabled();
  await page.getByLabel('补充风格偏好').fill('动作已传达情绪时，不追加解释性尾句。');
  const confirmed = page.waitForResponse(response => response.url().endsWith(
    base + `/feedback/preferences/${candidate.id}/confirm`));
  await confirm.click();
  expect((await confirmed).status()).toBe(200);
  await expect(page.getByLabel('补充风格偏好')).toHaveCount(0);
  const active = await (await request.get(base + '/styles/active-context')).json();
  expect(active.rules).toHaveLength(1);
  expect(active.rules[0].instruction).toBe('动作已传达情绪时，不追加解释性尾句。');
  expect((await request.delete(base + `/library/style_rule/${active.rules[0].id}`)).status()).toBe(200);
  await confirm.click();
  await expect(page.getByRole('alert')).toContainText('回收站');
  expect((await (await request.get(base + '/styles/active-context')).json()).rules).toHaveLength(0);
});
