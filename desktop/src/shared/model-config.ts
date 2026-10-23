import type { Json, JsonObject } from './bridge';

export interface ModelChoice {
  id: string;
  title: string;
  external: boolean;
  patch: JsonObject;
  minimum: JsonObject;
}
export function record(value: Json | undefined): JsonObject {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}
export function integer(value: Json | undefined): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}
export function usd(value: Json | undefined): string {
  const amount = integer(value);
  if (amount === undefined) return '未知';
  const micro = BigInt(amount);
  return `USD ${(micro / 1_000_000n).toLocaleString('en-US')}.${String(micro % 1_000_000n).padStart(6, '0')}`;
}
export function parseUsd(value: string): number {
  const input = value.trim();
  if (input.length > 32 || !/^\d+(?:\.\d{1,6})?$/.test(input)) throw new Error('请输入 USD 金额，例如 1.00；最多 6 位小数');
  const [whole, fraction = ''] = input.split('.');
  const amount = BigInt(whole!) * 1_000_000n + BigInt(fraction.padEnd(6, '0'));
  if (amount <= 0n || amount > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error('USD 上限须大于 0，且不能超过安全整数范围');
  return Number(amount);
}
export function jsonObject(text: string, label: string): JsonObject {
  let value: Json;
  try { value = JSON.parse(text) as Json; } catch { throw new Error(`${label}不是有效 JSON，请修正后再提交`); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label}必须是 JSON object`);
  return value;
}
export function modelChoices(capabilities: JsonObject | null): ModelChoice[] {
  const items = capabilities?.models;
  if (Array.isArray(items)) return items.flatMap(item => {
    const value = record(item), patch = record(value.config_patch);
    if (typeof value.id !== 'string' || patch.model !== value.id || typeof value.title !== 'string') return [];
    const external = value.external === true;
    if (external && value.currency !== 'USD') return [];
    return [{ id: value.id, title: value.title, external, patch, minimum: record(value.minimum_reservation) }];
  });
  return record(capabilities?.default_config).model === 'mock-research/v1'
    ? [{ id: 'mock-research/v1', title: 'Mock · 离线', external: false, patch: { model: 'mock-research/v1' }, minimum: {} }] : [];
}
export function buildRunConfig(base: JsonObject, choice: ModelChoice, budgetText: string, money: string): JsonObject {
  const limits = budgetText.trim() ? jsonObject(budgetText, '预算') : { ...record(base.limits) };
  for (const [key, value] of Object.entries(limits)) {
    if (key === 'max_cost_micro_usd' && value === null) continue;
    if (integer(value) === undefined) throw new Error(`预算 ${key} 必须是非负安全整数`);
  }
  if (choice.external) {
    const cost = parseUsd(money);
    if (limits.max_cost_micro_usd !== undefined && limits.max_cost_micro_usd !== null && limits.max_cost_micro_usd !== cost) {
      throw new Error('预算 JSON 的费用上限与 USD 输入不一致，请删除该字段或填写相同值');
    }
    limits.max_cost_micro_usd = cost;
  }
  const config: JsonObject = { ...base, ...choice.patch, limits };
  if (!choice.external) delete config.openai;
  return config;
}
export function liveIsExternal(run: JsonObject | null, overrides: string): boolean {
  try {
    const root = jsonObject(overrides, '干预');
    if (root.config !== undefined && root.config !== null) return record(root.config).model !== undefined && record(root.config).model !== 'mock-research/v1';
  } catch { /* 在提交时给出 JSON 错误，保留当前来源说明。 */ }
  return run?.model_external === true || typeof run?.model === 'string' && run.model.startsWith('openai/');
}
export function liveBudget(run: JsonObject | null, overrides: JsonObject, text: string, money: string): JsonObject {
  const base = overrides.config !== undefined && overrides.config !== null ? record(record(overrides.config).limits) : record(record(run?.budget).limits);
  const budget = text.trim() ? jsonObject(text, '独立预算') : { ...base };
  const cost = parseUsd(money);
  if (budget.max_cost_micro_usd !== undefined && text.trim() && budget.max_cost_micro_usd !== cost) {
    throw new Error('独立预算 JSON 的费用上限与 USD 输入不一致');
  }
  return { ...budget, max_cost_micro_usd: cost };
}
