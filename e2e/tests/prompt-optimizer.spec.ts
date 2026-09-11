import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext, type Locator, type Page, type Request } from '@playwright/test';

const idea = '打磨润色文章';
const scope = '当前第一章已保存正文';
const constraints = '保留人物语气、核心事件和伏笔，不新增设定';
const delivery = '给出修改建议与改写示例，由作者决定是否采纳';

async function createProject(request: APIRequestContext, title: string) {
  // The suite must run through the isolated Demo configuration, never an author server.
  const settings = await (await request.get('/api/v1/settings/model')).json();
  expect(settings.mode).toBe('demo');
  const response = await request.post('/api/v1/projects', { data: { title } });
  expect(response.status()).toBe(201);
  const project = await response.json();
  const base = `/api/v1/projects/${project.id}`;
  const created = await request.post(`${base}/nodes`, { data: { kind: 'chapter', title: '第一章 · 来信', order_index: 1 } });
  expect(created.status()).toBe(201);
  const chapter = await created.json();
  const document = await (await request.get(`${base}/chapters/${chapter.id}`)).json();
  expect((await request.put(`${base}/chapters/${chapter.id}`, {
    data: { content: '邮差敲了两次门，屋里只回应一声钟响。', contract: {}, revision: document.revision },
  })).status()).toBe(200);
  return { project, chapter, base };
}

function recordOptimizerRequests(page: Page) {
  const requests: string[] = [];
  const record = (request: Request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/')) requests.push(`${request.method()} ${url.pathname}`);
  };
  page.on('request', record);
  return () => { page.off('request', record); return requests; };
}

async function inputToStableOutput(page: Page, field: Locator, value: string, kind: 'hint' | 'preview') {
  const element = await field.elementHandle();
  if (!element) throw new Error('The measured input must be present before timing starts.');
  try {
    // Both endpoints of this measurement run inside the browser. No Playwright
    // round-trip, locator waiting or screenshot time is part of the elapsed value.
    return await page.evaluate(({ element, value, kind }) => new Promise<number>((resolve, reject) => {
      const input = element as HTMLInputElement | HTMLTextAreaElement;
      const read = () => kind === 'preview'
        ? document.querySelector('[role="dialog"][aria-label="完善提示词"] [aria-label="优化后的提示词"] pre')?.textContent ?? ''
        : Array.from(document.querySelectorAll('p,[role="status"]')).find(node =>
          node.textContent?.includes('这些表达可以更具体：') && (node as HTMLElement).getClientRects().length > 0)?.textContent ?? '';
      const expected = kind === 'preview' ? value : '这些表达可以更具体：';
      let previous = '', stableFrames = 0, frame = 0;
      const start = performance.now();
      const timeout = window.setTimeout(() => { cancelAnimationFrame(frame); reject(new Error('Local prompt output did not stabilize within 2500 ms.')); }, 2500);
      const check = () => {
        const output = read();
        stableFrames = output.includes(expected) && output === previous ? stableFrames + 1 : 0;
        previous = output;
        if (stableFrames >= 2) { clearTimeout(timeout); resolve(performance.now() - start); }
        else frame = requestAnimationFrame(check);
      };
      const prototype = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      frame = requestAnimationFrame(check);
    }), { element, value, kind });
  } finally { await element.dispose(); }
}

async function fillOptimizer(page: Page, goal = '减少重复叙述，让对话自然') {
  await page.getByRole('button', { name: '完善提示词', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '完善提示词', exact: true });
  await dialog.getByLabel('提示词模板', { exact: true }).selectOption('polish');
  await dialog.getByLabel('具体目标', { exact: true }).fill(goal);
  await dialog.getByLabel('处理范围', { exact: true }).fill(scope);
  await dialog.getByLabel('保留与限制', { exact: true }).fill(constraints);
  await dialog.getByLabel('交付形式', { exact: true }).fill(delivery);
  const preview = dialog.getByRole('region', { name: '优化后的提示词', exact: true }).locator('pre');
  for (const fragment of [idea, goal, scope, constraints, delivery]) await expect(preview).toContainText(fragment);
  return { dialog, preview };
}

async function unavailable(button: Locator) {
  await expect.poll(async () => (await button.count()) === 0 || !(await button.isEnabled())).toBe(true);
}

test('ambiguous prompts improve locally with responsive comparison, keyboard access, reversible application and browser-timed previews', async ({ page, request }, testInfo) => {
  const { project, chapter, base } = await createProject(request, '提示词验收 · 本地补全');
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  const composer = page.getByLabel('给 AI 的消息', { exact: true });
  await expect(composer).toBeVisible();
  await page.waitForLoadState('networkidle');
  // Start recording only after normal project loading has settled.
  const stopRecording = recordOptimizerRequests(page);
  const samples: Array<{ step: string; width: number; milliseconds: number }> = [];
  const hintMs = await inputToStableOutput(page, composer, idea, 'hint');
  samples.push({ step: 'ambiguity-hint', width: page.viewportSize()!.width, milliseconds: hintMs });
  expect(hintMs).toBeLessThan(2000);
  await expect(page.getByRole('dialog', { name: '完善提示词', exact: true })).toHaveCount(0);
  const { dialog, preview } = await fillOptimizer(page);
  await expect(dialog.getByRole('region', { name: '原始想法', exact: true })).toContainText(idea);
  const apply = dialog.getByRole('button', { name: '应用到输入框', exact: true });
  const close = dialog.getByRole('button', { name: '关闭对话框', exact: true });
  for (const [index, width] of [1440, 390, 320].entries()) {
    await page.setViewportSize({ width, height: 900 });
    const goal = `减少重复叙述，让对话自然；优先检查第 ${index + 1} 段`;
    const milliseconds = await inputToStableOutput(page, dialog.getByLabel('具体目标', { exact: true }), goal, 'preview');
    samples.push({ step: 'goal-to-preview', width, milliseconds });
    expect(milliseconds).toBeLessThan(2000);
    await expect(preview).toContainText(idea);
    await expect(preview).toContainText(scope);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await expect.poll(() => dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    await apply.focus(); await page.keyboard.press('Tab'); await expect(close).toBeFocused();
    await page.keyboard.press('Shift+Tab'); await expect(apply).toBeFocused();
    await page.screenshot({ path: `../.verification/prompt-optimizer-${width}.png`, fullPage: true, animations: 'disabled' });
    await dialog.evaluate(element => { element.scrollTop = 0; });
    await expect(dialog.getByRole('heading', { name: '完善提示词', exact: true })).toBeInViewport();
    await page.screenshot({ path: `../.verification/prompt-optimizer-${width}-top.png`, fullPage: true, animations: 'disabled' });
    await preview.scrollIntoViewIfNeeded();
    await expect(preview).toBeInViewport();
    await page.screenshot({ path: `../.verification/prompt-optimizer-${width}-comparison.png`, fullPage: true, animations: 'disabled' });
  }
  await page.keyboard.press('Escape');
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole('button', { name: '完善提示词', exact: true })).toBeFocused();
  await expect(composer).toHaveValue(idea);

  const reopened = await fillOptimizer(page);
  let optimized = await reopened.preview.innerText();
  await reopened.dialog.getByRole('button', { name: '应用到输入框', exact: true }).click();
  await expect(reopened.dialog).not.toBeVisible();
  await expect(composer).toHaveValue(optimized);
  await page.getByRole('button', { name: '撤销提示词优化', exact: true }).click();
  await expect(composer).toHaveValue(idea);
  await page.getByRole('button', { name: '恢复提示词优化', exact: true }).click();
  await expect(composer).toHaveValue(optimized);

  // Editing an applied optimization must revise its answers, not wrap the
  // already-optimized result in a second task/author block.
  await page.getByRole('button', { name: '完善提示词', exact: true }).click();
  const revisionDialog = page.getByRole('dialog', { name: '完善提示词', exact: true });
  const revisionPreview = revisionDialog.getByRole('region', { name: '优化后的提示词', exact: true }).locator('pre');
  await expect(revisionDialog.getByRole('region', { name: '原始想法', exact: true }).locator('pre')).toHaveText(idea);
  await expect(revisionDialog.getByLabel('具体目标', { exact: true })).toHaveValue('减少重复叙述，让对话自然');
  await expect(revisionDialog.getByLabel('处理范围', { exact: true })).toHaveValue(scope);
  await expect(revisionPreview).toHaveText(optimized);
  await expect(revisionDialog.getByRole('button', { name: '应用到输入框', exact: true })).toBeDisabled();
  const previousOptimization = optimized;
  await revisionDialog.getByLabel('具体目标', { exact: true }).fill('减少重复叙述，优先梳理结尾的动作顺序');
  optimized = await revisionPreview.innerText();
  expect(optimized.match(/【任务说明】/g)).toHaveLength(1);
  expect(optimized.match(/【作者补充】/g)).toHaveLength(1);
  await revisionDialog.getByRole('button', { name: '应用到输入框', exact: true }).click();
  await expect(composer).toHaveValue(optimized);
  await page.getByRole('button', { name: '撤销提示词优化', exact: true }).click();
  await expect(composer).toHaveValue(previousOptimization);
  await page.getByRole('button', { name: '恢复提示词优化', exact: true }).click();
  await expect(composer).toHaveValue(optimized);
  const manuallyEdited = optimized + '\n作者补充：仅修改第一段。';
  await composer.fill(manuallyEdited);
  await unavailable(page.getByRole('button', { name: '撤销提示词优化', exact: true }));
  await unavailable(page.getByRole('button', { name: '恢复提示词优化', exact: true }));
  await expect(composer).toHaveValue(manuallyEdited);

  // Use Playwright's browser input path: an HTML maxLength must not silently
  // truncate a pasted draft before the application's explicit length guard runs.
  const overlong = '原'.repeat(16000) + '末尾不得丢失';
  await composer.fill(overlong);
  await expect(composer).toHaveValue(overlong);
  await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeDisabled();
  await composer.press('Enter');
  await expect(composer).toHaveValue(overlong);
  await page.getByRole('button', { name: '完善提示词', exact: true }).click();
  const overlongDialog = page.getByRole('dialog', { name: '完善提示词', exact: true });
  await expect(overlongDialog.getByText('原文超过 16,000 字符，请先缩短输入；不会自动截断原文。', { exact: true })).toBeVisible();
  await expect(overlongDialog.getByRole('region', { name: '原始想法', exact: true }).locator('pre')).toHaveText(overlong);
  await expect(overlongDialog.getByRole('button', { name: '应用到输入框', exact: true })).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(composer).toHaveValue(overlong);
  expect(stopRecording()).toEqual([]);
  expect((await (await request.get(`${base}/ai/jobs/page?chapter_id=${chapter.id}`)).json()).items).toEqual([]);
  console.log('[prompt-optimizer-performance] ' + JSON.stringify(samples));
  await testInfo.attach('prompt-optimizer-performance', { body: JSON.stringify(samples, null, 2), contentType: 'application/json' });
});

test('optimized drafts stay isolated between real conversations, survive navigation and require explicit recovery after reload', async ({ page, request }) => {
  const { project, chapter, base } = await createProject(request, '提示词验收 · 会话草稿隔离');
  const threads: Array<{ id: string; title: string }> = [];
  for (const title of ['提示词方案甲', '提示词方案乙']) {
    const response = await request.post(`${base}/conversations`, { data: { id: randomUUID(), chapter_id: chapter.id, title } });
    expect(response.status()).toBe(201); threads.push(await response.json());
  }
  await page.goto('/');
  await page.getByLabel('当前小说项目').selectOption(project.id);
  await page.locator('details.conversation-management > summary').click();
  const conversations = page.getByRole('region', { name: '会话管理', exact: true });
  await conversations.getByRole('button', { name: /提示词方案甲.*条消息/ }).click();
  await expect(conversations.getByText('当前：提示词方案甲', { exact: true })).toBeVisible();
  await page.waitForLoadState('networkidle');
  const composer = page.getByLabel('给 AI 的消息', { exact: true });
  const stopRecording = recordOptimizerRequests(page);
  await composer.fill(idea);
  const { dialog, preview } = await fillOptimizer(page);
  const optimized = await preview.innerText();
  await dialog.getByRole('button', { name: '应用到输入框', exact: true }).click();
  await expect(composer).toHaveValue(optimized);
  expect(stopRecording()).toEqual([]);
  // Scope navigation has its own legitimate GETs and is outside the optimizer recording window.
  await conversations.getByRole('button', { name: /提示词方案乙.*条消息/ }).click();
  await expect(composer).toHaveValue('');
  await unavailable(page.getByRole('button', { name: '撤销提示词优化', exact: true }));
  await composer.fill('仅会话乙保留的独立消息');
  await conversations.getByRole('button', { name: /提示词方案甲.*条消息/ }).click();
  await expect(composer).toHaveValue(optimized);
  const acceptUnload = (dialog: import('@playwright/test').Dialog) => { void dialog.accept(); };
  page.on('dialog', acceptUnload);
  try { await page.reload(); } finally { page.off('dialog', acceptUnload); }
  await expect(composer).toHaveValue('');
  await page.getByRole('button', { name: '恢复聊天草稿', exact: true }).click();
  await expect(composer).toHaveValue(optimized);
  await unavailable(page.getByRole('button', { name: '撤销提示词优化', exact: true }));
  await page.locator('details.conversation-management > summary').click();
  await expect(conversations.getByText('当前：提示词方案甲', { exact: true })).toBeVisible();
  await conversations.getByRole('button', { name: /提示词方案乙.*条消息/ }).click();
  await expect(composer).toHaveValue('');
  await page.getByRole('button', { name: '恢复聊天草稿', exact: true }).click();
  await expect(composer).toHaveValue('仅会话乙保留的独立消息');
  for (const thread of threads) expect((await (await request.get(`${base}/ai/jobs/page?chapter_id=${chapter.id}&conversation_id=${thread.id}`)).json()).items).toEqual([]);
  expect((await (await request.get(`${base}/quality/runs`)).json()).items).toEqual([]);
});

for (const shortcut of ['Control+k', 'Meta+k']) {
  test(`prompt optimizer keeps modal focus when ${shortcut} is pressed and restores the reference shortcut after closing`, async ({ page, request }) => {
    const { project } = await createProject(request, `提示词验收 · 快捷键隔离 ${shortcut}`);
    await page.goto('/');
    await page.getByLabel('当前小说项目').selectOption(project.id);
    const composer = page.getByLabel('给 AI 的消息', { exact: true });
    await expect(composer).toBeVisible();
    await composer.fill(idea);
    const { dialog } = await fillOptimizer(page);
    const goal = dialog.getByLabel('具体目标', { exact: true });
    await goal.focus();
    await page.keyboard.press(shortcut);
    await expect(page.getByRole('dialog')).toHaveCount(1);
    await expect(page.getByRole('dialog', { name: '参考资料', exact: true })).toHaveCount(0);
    await expect(goal).toBeFocused();
    await expect.poll(() => page.evaluate(() => document.getElementById('root')?.inert)).toBe(true);

    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible();
    await expect(page.getByRole('button', { name: '完善提示词', exact: true })).toBeFocused();
    await expect.poll(() => page.evaluate(() => document.getElementById('root')?.inert)).toBe(false);
    await expect(composer).toHaveValue(idea);

    await composer.focus();
    await page.keyboard.press(shortcut);
    const references = page.getByRole('dialog', { name: '参考资料', exact: true });
    await expect(references).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(1);
    await page.keyboard.press('Escape');
    await expect(references).not.toBeVisible();
    await expect(composer).toBeFocused();
    await expect.poll(() => page.evaluate(() => document.getElementById('root')?.inert)).toBe(false);
  });
}
