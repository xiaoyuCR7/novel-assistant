import { describe, expect, it, vi } from 'vitest';
import {
  analyzePrompt, buildOptimizedPrompt, PROMPT_MAX_LENGTH, promptTemplates,
  type PromptAnswers, type PromptTemplateId,
} from '../src/features/ai/promptOptimizerEngine';

const empty: PromptAnswers = { goal: '', scope: '', constraints: '', output: '' };

describe('local prompt analysis', () => {
  it.each(['打磨润色文章', '优化内容', '帮我把文章改得好一点', '让文字更吸引人'])('identifies vague instructions: %s', source => {
    const result = analyzePrompt(source);
    expect(result.phrases.length).toBeGreaterThan(0);
    expect(result.missing).toEqual(['goal', 'scope']);
    expect(result.suggestedTemplate).toBe('polish');
    expect(result.tooLong).toBe(false);
    expect(result.followUp).toBe(false);
  });

  it.each(['继续', '请继续。', '好的', '好，继续吧！', '谢谢', '好的，谢谢！'])('leaves conversational follow-ups alone: %s', source => {
    expect(analyzePrompt(source)).toMatchObject({ phrases: [], missing: [], followUp: true, tooLong: false });
  });

  it('only asks for the missing part of a vague editing instruction', () => {
    expect(analyzePrompt('请润色第2章最后两段，让它更吸引人。').missing).toEqual(['goal']);
    expect(analyzePrompt('优化内容：删除重复比喻，修正错别字。').missing).toEqual(['scope']);
  });

  it.each<[string, PromptTemplateId]>([
    ['请润色第2章最后两段，删除重复比喻，只输出修改后的段落。', 'polish'],
    ['请续写下一章，接住上一章的悬念，不增加新人物。', 'continue'],
    ['根据当前大纲规划第三卷的章节顺序，用列表列出每章目标。', 'plan'],
    ['检查第2章人物称谓与时间线是否一致，逐条列出依据。', 'review'],
    ['讨论当前主角拒绝赴约的动机，提供三个可能方向，不要写正文。', 'brainstorm'],
  ])('recognizes concrete instructions without forcing extra fields: %s', (source, template) => {
    expect(analyzePrompt(source)).toMatchObject({ missing: [], suggestedTemplate: template, followUp: false });
  });

  it('does not mistake a negated suggestion for an editing request', () => {
    const source = '不要润色，不要让文章更吸引人；只修正第2章错别字。';
    expect(analyzePrompt(source)).toMatchObject({ phrases: [], missing: [], suggestedTemplate: 'custom' });
  });

  it.each<[string, PromptTemplateId]>([
    ['请根据已有大纲续写第二章，不新增人物。', 'continue'],
    ['请润色第二章，检查并修正病句。', 'polish'],
    ['根据人物设定核对的结果，继续写第二章。', 'continue'],
    ['检查第二章润色的结果，不要改写正文。', 'review'],
    ['请讨论续写第二章的三种可能方向，不要写正文。', 'brainstorm'],
  ])('selects the requested action rather than a reference or secondary step: %s', (source, template) => {
    expect(analyzePrompt(source).suggestedTemplate).toBe(template);
  });

  it.each(['不润色文章，只修正第二章错别字。', '不是要润色文章，只修正第二章错别字。'])('respects a directly negated action: %s', source => {
    expect(analyzePrompt(source)).toMatchObject({ phrases: [], missing: [], suggestedTemplate: 'custom' });
  });

  it.each(['不是要规划大纲，而是续写第二章。', '不是要规划大纲而是续写第二章。'])('uses the affirmative action after a correction: %s', source => {
    expect(analyzePrompt(source).suggestedTemplate).toBe('continue');
  });

  it.each(['不仅要润色第二章，还要修正病句。', '不只要润色第二章，还要修正病句。', '不得不润色第二章，修正病句。'])('keeps affirmative expressions containing 不: %s', source => {
    expect(analyzePrompt(source)).toMatchObject({ phrases: ['润色'], missing: [], suggestedTemplate: 'polish' });
  });

  it('does not count an excluded chapter as the requested scope', () => {
    expect(analyzePrompt('优化内容，修正病句，不要修改第一章。').missing).toEqual(['scope']);
    expect(analyzePrompt('优化内容，修正病句，不修改第一章。').missing).toEqual(['scope']);
    expect(analyzePrompt('优化第二章，修正病句，不要修改第一章。').missing).toEqual([]);
    expect(analyzePrompt('优化内容，修正病句，不只修改第一章。').missing).toEqual([]);
  });

  it('uses a pasted text or explicit source reference as a scope without claiming semantic certainty', () => {
    expect(analyzePrompt('润色选中的段落，让它更吸引人。').missing).toEqual(['goal']);
    expect(analyzePrompt('润色 [[ref:document:chapter-1]]，删除重复比喻。').missing).toEqual([]);
    expect(analyzePrompt('润色下面的正文，修正病句：\n\n她走进门，门外的雨还没停。').missing).toEqual([]);
    expect(analyzePrompt('如果换一种方式理解他的选择，会怎样？').missing).toEqual([]);
  });

  it('suggests the essentials for empty input without fabricating a task', () => {
    expect(analyzePrompt('  \n')).toMatchObject({ phrases: [], missing: ['goal', 'scope'], suggestedTemplate: 'custom' });
  });

  it('skips analysis above the input limit and handles the boundary efficiently', () => {
    expect(PROMPT_MAX_LENGTH).toBe(16000);
    expect(analyzePrompt('润'.repeat(PROMPT_MAX_LENGTH + 1))).toEqual({
      phrases: [], missing: [], suggestedTemplate: 'custom', tooLong: true, followUp: false,
    });
    const source = `${'不'.repeat(PROMPT_MAX_LENGTH - 4)}润色文章`;
    const start = performance.now();
    for (let run = 0; run < 20; run++) expect(analyzePrompt(source).tooLong).toBe(false);
    expect(performance.now() - start).toBeLessThan(2000);
    expect(analyzePrompt('🌧️'.repeat(PROMPT_MAX_LENGTH)).tooLong).toBe(true);
  });
});

describe('prompt assembly', () => {
  it('offers six distinct templates and optional suggestions for every field', () => {
    expect(promptTemplates.map(item => item.id).sort()).toEqual(['brainstorm', 'continue', 'custom', 'plan', 'polish', 'review']);
    for (const template of promptTemplates) {
      expect(template.label.trim()).not.toBe('');
      expect(template.description.trim()).not.toBe('');
      for (const field of ['goal', 'scope', 'constraints', 'output'] as const) {
        expect(template.suggestions[field].length).toBeGreaterThan(0);
        expect(template.suggestions[field].every(text => text.trim().length > 0)).toBe(true);
      }
    }
  });

  it.each<PromptTemplateId>(['custom', 'polish', 'continue', 'plan', 'review', 'brainstorm'])('preserves source text exactly in %s, including Unicode, JSON, variables and prohibitions', template => {
    const source = '  不要改变叙事视角，也不要新增人物。\r\n{"scene":"{{scene}}","emoji":"👩🏽‍🚀","quote":"\\n"}\n\n保留 e\u0301 和𝄞。  ';
    const answers = { ...empty, goal: '  修正病句\n保留 {{voice}}  ', constraints: '不得替换 JSON 字段。' };
    const result = buildOptimizedPrompt(source, template, answers);
    expect(result.slice(0, source.length)).toBe(source);
    expect(result).toContain(answers.goal);
    expect(result).toContain(answers.constraints);
    expect(result).not.toContain('undefined');
    expect(answers).toEqual({ ...empty, goal: '  修正病句\n保留 {{voice}}  ', constraints: '不得替换 JSON 字段。' });
  });

  it('only appends selected task guidance and supplied answers, without invented story defaults', () => {
    const source = '优化这段内容，不要替我决定字数。';
    const result = buildOptimizedPrompt(source, 'polish', { ...empty, scope: '第三章末尾的两段' });
    expect(result).toContain('第三章末尾的两段');
    expect(result).not.toContain('目标：');
    expect(result).not.toMatch(/\d+\s*字|第[一二四五六七八九十]章|林渡|女主角|男主角/);
    expect(result).not.toContain(promptTemplates.find(item => item.id === 'polish')!.suggestions.goal[0]);
  });

  it('keeps overlong source and answers intact for the UI to reject without truncation', () => {
    const source = `${'原'.repeat(PROMPT_MAX_LENGTH)}末尾不得丢失`;
    const goal = `${'补'.repeat(PROMPT_MAX_LENGTH)}作者要求末尾`;
    const result = buildOptimizedPrompt(source, 'custom', { ...empty, goal });
    expect(result.startsWith(source)).toBe(true);
    expect(result).toContain(goal);
    expect(result.length).toBeGreaterThan(PROMPT_MAX_LENGTH);
  });

  it('is deterministic, skips blank answers and performs no network calls', () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(() => { throw Error('unexpected network'); });
    try {
      const source = '讨论主角的选择。';
      const result = buildOptimizedPrompt(source, 'brainstorm', empty);
      expect(buildOptimizedPrompt(source, 'brainstorm', { ...empty, scope: ' \n ' })).toBe(result);
      expect(analyzePrompt(source)).toEqual(analyzePrompt(source));
      expect(fetcher).not.toHaveBeenCalled();
    } finally { fetcher.mockRestore(); }
  });
});
