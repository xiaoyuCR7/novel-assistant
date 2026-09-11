export type PromptTemplateId = 'custom' | 'polish' | 'continue' | 'plan' | 'review' | 'brainstorm';
export type PromptFieldId = 'goal' | 'scope' | 'constraints' | 'output';
export type PromptAnswers = Record<PromptFieldId, string>;
export const PROMPT_MAX_LENGTH = 16000;

export const promptTemplates: Array<{
  id: PromptTemplateId;
  label: string;
  description: string;
  suggestions: Record<PromptFieldId, string[]>;
}> = [
  {
    id: 'custom', label: '自定义', description: '保留你的任务，用少量补充说明想达成什么。',
    suggestions: {
      goal: ['先澄清问题，再提出可执行的建议', '围绕我指出的问题给出解决办法'],
      scope: ['仅处理我提供的材料', '围绕当前讨论的主题'],
      constraints: ['不要代替我确定未说明的设定', '保留原始输入中的否定约束'],
      output: ['先给结论，再说明理由', '用简洁列表呈现结果'],
    },
  },
  {
    id: 'polish', label: '正文润色', description: '说明要改善的文字问题与可以调整的范围。',
    suggestions: {
      goal: ['修正病句，减少重复表达', '让对话更符合人物身份与当下情绪'],
      scope: ['仅修改选中的段落', '处理当前章节的正文'],
      constraints: ['保留剧情事实、人物关系和叙事视角', '不新增事件，不提前揭示伏笔'],
      output: ['给出修改后的文本与简短修改说明', '逐项列出问题与对应修改建议'],
    },
  },
  {
    id: 'continue', label: '正文续写', description: '交代从哪里接续、要推进什么，以及暂时不能展开什么。',
    suggestions: {
      goal: ['承接已有悬念，推进当前场景的冲突', '通过人物行动推动情节发展'],
      scope: ['从当前正文末尾继续', '按已有大纲推进下一章'],
      constraints: ['遵守已确认设定与章节合同', '不提前揭示尚未到时机的秘密'],
      output: ['输出接续正文，避免复述已有内容', '先列接续思路，确认后再写正文'],
    },
  },
  {
    id: 'plan', label: '情节规划', description: '把情节方向整理为可讨论的计划，保留作者决定权。',
    suggestions: {
      goal: ['梳理事件因果与人物行动动机', '安排冲突推进与信息揭示的节奏'],
      scope: ['规划当前章节的场景顺序', '围绕我提供的大纲调整后续安排'],
      constraints: ['区分已确认设定与新增建议', '不把建议当作已经发生的情节'],
      output: ['用列表说明各阶段目标与承接关系', '给出备选方向及其取舍'],
    },
  },
  {
    id: 'review', label: '一致性检查', description: '对照已提供的正文与依据找问题，标清不确定之处。',
    suggestions: {
      goal: ['核对人物称谓、时间线与已确认设定', '检查因果衔接与前后矛盾'],
      scope: ['检查当前章节及我指定的参考资料', '对照我选中的前后段落'],
      constraints: ['依据不足时说明无法判断', '引用原文位置，不直接改写正文'],
      output: ['列出问题、依据、影响与修改建议', '区分已发现的问题和需要作者确认的疑点'],
    },
  },
  {
    id: 'brainstorm', label: '创作讨论', description: '探索人物与故事的可能性，暂不写入正式设定。',
    suggestions: {
      goal: ['探索人物选择背后的动机', '比较不同情节方向的吸引力与代价'],
      scope: ['围绕当前讨论的角色或场景', '仅讨论我提出的故事问题'],
      constraints: ['所有新增内容都作为备选建议', '不要直接生成或改写正文'],
      output: ['列出不同思路及各自适用条件', '先提出关键问题，再给可讨论的方向'],
    },
  },
];

const guidance: Record<PromptTemplateId, string> = {
  custom: '依据原始输入与作者补充完成任务。会影响结果的重要信息没有说明时，先简短询问，不自行设定。',
  polish: '在作者指定的范围内，围绕其目标改善文字。除非作者明确要求，保留剧情事实、人物关系与叙事视角；不擅自新增事件或揭示伏笔。',
  continue: '从作者指定的位置接续正文，结合已有上下文与已确认设定推进作者提出的目标。起点或走向不足以确定时先询问，不自行决定篇幅与章节范围。',
  plan: '围绕作者指定的故事范围提出规划，说明事件承接、人物动机与待确认事项。区分已有事实和新的备选建议，不将未确认计划视为正式设定。',
  review: '在指定范围内，对照已提供的正文与设定检查一致性。说明发现问题的依据，区分可确认的问题与信息不足的疑点；没有依据时不声称完成全面核验。',
  brainstorm: '围绕作者的问题讨论不同可能性，说明每种方向的取舍。新增想法作为待选择的建议；除非作者明确要求，不代替作者决定正式剧情或生成正文。',
};

const followUps = new Set([
  '继续', '请继续', '继续吧', '请继续吧', '继续写', '请继续写', '接着写', '接着说',
  '好', '好的', '好继续', '好的继续', '好继续吧', '好的继续吧', '可以', '行',
  '收到', '明白', '明白了', '谢谢', '谢谢你', '感谢', '好的谢谢', '好谢谢', '好的谢谢你',
]);

// These are conservative wording clues, not a semantic assessment of the prompt.
const vague = /打磨润色|润色打磨|(?:润色|打磨)(?:一下|下)?(?:文章|正文|内容|文本|文字)?|优化(?:一下|下)?(?:内容|文章|正文|文本|文字)?|更吸引人|吸引力更强|更好|更精彩|更生动|更流畅|好一点|好一些/gu;
const concreteGoal = /(?:修正|纠正|改正|检查|找出|标出)[^，。！？；\n]{0,12}(?:错别字|病句|标点|语法|称谓|时间线|视角|冲突|矛盾|逻辑|重复)|(?:删除|删去|删掉|合并|减少|消除)[^，。！？；\n]{0,12}(?:重复|赘述|冗余|比喻|形容词)|(?:缩短|压缩|扩写|扩展|改写)[^，。！？；\n]{0,12}(?:字|句|段|对话|描写)|(?:统一|转换|改为|改成)[^，。！？；\n]{0,12}(?:人称|视角|时态|语气|称谓)|(?:突出|补充|解释|交代|强化)[^，。！？；\n]{0,12}(?:动机|因果|冲突|动作|线索|悬念)/gu;
const scope = /第[一二三四五六七八九十百千万零〇两\d]{1,16}[章卷节段句]|(?:当前|本|上[一]?|下[一]?|前[一]?|后[一]?)[章卷节段句]|(?:选中|以下|下面|下列|所附|引用|这段|这篇|这句|该段|该章|这章)|\[\[ref:[^\]\r\n]{1,160}\]\]/gu;

function isNegated(source: string, index: number): boolean {
  // Limit the look-behind to a single short clause; never scan backwards through a document.
  const prefix = source.slice(Math.max(0, index - 24), index)
    .split(/而是|但是|不过|但/u).at(-1)!
    .replace(/不仅|不只|不得不/gu, '');
  return /(?:不要|不必|不需要|无需|不用|别|勿|禁止|不允许|不能|不是|不)[^，。！？；：\n]{0,12}$/u.test(prefix);
}

function positiveMatches(source: string, pattern: RegExp): string[] {
  return [...new Set(Array.from(source.matchAll(pattern))
    .filter(match => !isNegated(source, match.index))
    .map(match => match[0]))];
}

function suggestTemplate(source: string, hasVagueWording: boolean): PromptTemplateId {
  // Prefer the first explicit action over topic nouns (e.g. an outline used for continuation).
  const actions: Array<[PromptTemplateId, RegExp]> = [
    ['review', /审校|检查|核对/gu],
    ['plan', /规划/gu],
    ['continue', /续写|接着写|继续写|接续/gu],
    ['brainstorm', /讨论|头脑风暴|构思/gu],
    ['polish', /润色|打磨|改写/gu],
  ];
  let firstAction: { template: PromptTemplateId; index: number } | undefined;
  for (const [template, pattern] of actions) {
    for (const match of source.matchAll(pattern)) {
      if (isNegated(source, match.index)) continue;
      // "核对的结果" names existing material rather than requesting another review.
      if (/^(?:后)?的(?:结果|报告|记录)/u.test(source.slice(match.index + match[0].length, match.index + match[0].length + 8))) continue;
      if (!firstAction || match.index < firstAction.index) firstAction = { template, index: match.index };
      break;
    }
  }
  if (firstAction) return firstAction.template;
  const rules: Array<[PromptTemplateId, RegExp]> = [
    ['review', /一致性|审校|检查|核对|前后矛盾/gu],
    ['plan', /规划|大纲|章节安排|场景顺序/gu],
    ['continue', /续写|接着写|继续写|接续/gu],
    ['brainstorm', /讨论|头脑风暴|灵感|构思|备选方向|可能方向/gu],
    ['polish', /润色|打磨|优化|改写/gu],
  ];
  for (const [template, pattern] of rules) {
    if (positiveMatches(source, pattern).length) return template;
  }
  return hasVagueWording ? 'polish' : 'custom';
}

export function analyzePrompt(source: string): {
  phrases: string[];
  missing: PromptFieldId[];
  suggestedTemplate: PromptTemplateId;
  tooLong: boolean;
  followUp: boolean;
} {
  const base = { phrases: [] as string[], missing: [] as PromptFieldId[], suggestedTemplate: 'custom' as PromptTemplateId,
    tooLong: false, followUp: false };
  // Check before trimming, matching or allocating copies of potentially oversized text.
  if (source.length > PROMPT_MAX_LENGTH) return { ...base, tooLong: true };
  if (!source.trim()) return { ...base, missing: ['goal', 'scope'] };
  if (source.length < 64 && followUps.has(source.replace(/[\s，,。.!！?？~～、]/gu, ''))) {
    return { ...base, followUp: true };
  }
  const phrases = positiveMatches(source, vague);
  const missing: PromptFieldId[] = [];
  // Only flag obvious vague requests. Unrecognized instructions remain usable as written.
  if (phrases.length) {
    if (!positiveMatches(source, concreteGoal).length) missing.push('goal');
    if (!positiveMatches(source, scope).length) missing.push('scope');
  }
  return { ...base, phrases, missing, suggestedTemplate: suggestTemplate(source, phrases.length > 0) };
}

export function buildOptimizedPrompt(source: string, templateId: PromptTemplateId, answers: PromptAnswers): string {
  const labels: Record<PromptFieldId, string> = { goal: '目标', scope: '范围', constraints: '约束', output: '输出方式' };
  const additions = (Object.keys(labels) as PromptFieldId[])
    .filter(field => answers[field].trim())
    .map(field => `${labels[field]}：\n${answers[field]}`);
  // Source and supplied answers are kept verbatim. The caller checks the final length before applying.
  return `${source}\n\n【任务说明】\n${guidance[templateId]}\n遵守原始输入中的明确要求与否定约束；补充或模板说明与之冲突时，先向作者确认，不自行取舍。`
    + (additions.length ? `\n\n【作者补充】\n${additions.join('\n\n')}` : '');
}
