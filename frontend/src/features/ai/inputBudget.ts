import type { TaskBudgetPreflight } from '../../lib/api';

export const DEFAULT_INPUT_BUDGET = 12000;
const sessionBudgets = new Map<string, string>();
const key = (projectId: string) => `studio:input-budget:${encodeURIComponent(projectId)}`;

export function inputBudgetValue(value: string): number {
  const budget = Number(value);
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(budget) || budget < 256 || budget > 200000)
    throw Error('输入预算必须是 256–200000 之间的整数。');
  return budget;
}

export function readInputBudget(projectId: string): string | undefined {
  try {
    const value = sessionBudgets.get(projectId) ?? localStorage.getItem(key(projectId));
    if (value == null) return undefined;
    inputBudgetValue(value);
    return value;
  } catch { return sessionBudgets.get(projectId); }
}

export function saveInputBudget(projectId: string, value: string): void {
  try { inputBudgetValue(value); } catch { return; }
  try {
    localStorage.setItem(key(projectId), value);
    sessionBudgets.delete(projectId);
  } catch { sessionBudgets.set(projectId, value); }
}

export function requireFittingPreflight(report: TaskBudgetPreflight, budget: number): void {
  if (!report || !Number.isSafeInteger(report.required_input_tokens) || report.required_input_tokens < 0
    || report.token_budget !== budget || !Number.isSafeInteger(report.output_token_budget)
    || report.output_token_budget < 256 || report.output_token_budget > 65536
    || !Number.isSafeInteger(report.context_capacity) || report.context_capacity < 1024
    || report.context_capacity > 1048576 || report.estimated !== true
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
