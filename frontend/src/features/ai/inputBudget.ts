import type { ModelConfig, TaskBudgetPreflight } from '../../lib/api';

export const MAX_INPUT_BUDGET = 1048576;
const sessionBudgets = new Map<string, string | null>();
const key = (projectId: string) => `studio:input-budget:${encodeURIComponent(projectId)}`;

export function inputBudgetValue(value: string): number {
  const budget = Number(value);
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(budget) || budget < 256 || budget > MAX_INPUT_BUDGET)
    throw Error(`输入预算必须是 256–${MAX_INPUT_BUDGET} 之间的整数。`);
  return budget;
}

export function modelInputBudget(config?: Pick<ModelConfig, 'context_capacity' | 'output_token_budget'>): number {
  const capacity = config?.context_capacity, output = config?.output_token_budget;
  if (typeof capacity !== 'number' || !Number.isSafeInteger(capacity) || capacity < 1024 || capacity > MAX_INPUT_BUDGET
    || typeof output !== 'number' || !Number.isSafeInteger(output) || output < 256 || output > 65536 || capacity - output < 256)
    throw Error('请在模型设置中填写有效的上下文容量与输出预留，并为输入保留至少 256 Token。');
  return capacity - output;
}

export function resolveInputBudget(config: Pick<ModelConfig, 'context_capacity' | 'output_token_budget'> | undefined, override?: string): number {
  const available = modelInputBudget(config);
  return override === undefined ? available : inputBudgetValue(override);
}

export function readInputBudget(projectId: string): string | undefined {
  try {
    const value = sessionBudgets.has(projectId) ? sessionBudgets.get(projectId) : localStorage.getItem(key(projectId));
    if (value == null) return undefined;
    inputBudgetValue(value);
    return value;
  } catch { return sessionBudgets.get(projectId) ?? undefined; }
}

export function saveInputBudget(projectId: string, value: string): void {
  try { inputBudgetValue(value); } catch { return; }
  try {
    localStorage.setItem(key(projectId), value);
    sessionBudgets.delete(projectId);
  } catch { sessionBudgets.set(projectId, value); }
}

export function clearInputBudget(projectId: string): void {
  try { localStorage.removeItem(key(projectId)); sessionBudgets.delete(projectId); }
  catch { sessionBudgets.set(projectId, null); }
}

export function requireFittingPreflight(report: TaskBudgetPreflight, budget: number): void {
  if (!report || !Number.isSafeInteger(report.required_input_tokens) || report.required_input_tokens < 0
    || !Number.isSafeInteger(budget) || budget < 256 || budget > MAX_INPUT_BUDGET
    || report.token_budget !== budget || !Number.isSafeInteger(report.output_token_budget)
    || report.output_token_budget < 256 || report.output_token_budget > 65536
    || !Number.isSafeInteger(report.context_capacity) || report.context_capacity < 1024
    || report.context_capacity > MAX_INPUT_BUDGET || report.context_capacity - report.output_token_budget < 256 || report.estimated !== true
    || report.scope !== 'first-stage-hard-only' || typeof report.message !== 'string' || !report.message
    || report.effective_input_limit !== Math.min(budget, report.context_capacity - report.output_token_budget)
    || report.can_fit !== (report.required_input_tokens <= report.effective_input_limit))
    throw Error('输入预算预检返回无效，未创建任务；请稍后重试。');
  if (!report.can_fit) throw Error(
    `必要输入约 ${report.required_input_tokens} Token，当前输入预算 ${report.token_budget}，`
    + `预留输出 ${report.output_token_budget}，模型容量 ${report.context_capacity}；`
    + `有效输入上限 ${report.effective_input_limit}。${report.message}`,
  );
}
