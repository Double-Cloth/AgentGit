import type { Json, JsonObject } from './bridge';
import { integer, jsonObject, parseUsd, record } from './model-config';

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const DIGEST = /^[0-9a-f]{64}$/;
export type GroupKind = 'inherit' | 'prompt' | 'plan' | 'interventions';
export type GroupDraft = { label: string; kind: GroupKind; prompt: string; plan: string; ids: string };
export type ExperimentScope = { sourceRunId: string; fromCommit: string; fixtureSplit: 'screen' | 'confirm' | 'regression' };
export const draft = (label: string): GroupDraft => ({ label, kind: 'inherit', prompt: '', plan: '', ids: '' });

export function hasFeature(capabilities: JsonObject | null, feature: string): boolean {
  return Array.isArray(capabilities?.features) && capabilities.features.includes(feature);
}
export function identifier(text: string, label: string): string {
  const value = text.trim();
  if (!UUID.test(value)) throw new Error(`${label}须为完整 UUID`);
  return value.toLowerCase();
}
export function digest(text: string, label: string): string {
  const value = text.trim();
  if (!DIGEST.test(value)) throw new Error(`${label}须为完整 64 位小写摘要`);
  return value;
}
export function eventId(text: string): number {
  const value = text.trim();
  if (!/^[1-9]\d{0,15}$/.test(value) || BigInt(value) > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error('目标事件 ID 须为安全范围内的正整数');
  return Number(value);
}
export function identifiers(text: string, label: string, maximum: number): string[] {
  const values = text.trim().split(/[\s,]+/).filter(Boolean).map(value => identifier(value, label));
  if (!values.length || values.length > maximum || new Set(values).size !== values.length) throw new Error(`${label}须为 1–${maximum} 个不同 UUID，可用逗号或换行分隔`);
  return values;
}
export function groupRequest(key: 'A' | 'B', input: GroupDraft): JsonObject {
  const label = input.label.trim();
  if (!label || label.length > 200) throw new Error(`${key} 组名称须为 1–200 字`);
  const group: JsonObject = { schema_version: 1, key, label };
  if (input.kind === 'prompt') {
    if (!input.prompt.trim() || input.prompt.length > 16000) throw new Error(`${key} 组须提供 1–16000 字的完整 Prompt`);
    group.prompt = input.prompt;
  } else if (input.kind === 'plan') group.plan = jsonObject(input.plan, `${key} 组计划`);
  else if (input.kind === 'interventions') group.interventions = identifiers(input.ids, `${key} 组干预`, 8);
  return group;
}
export function experimentBudget(run: JsonObject | null, text: string, money: string): JsonObject {
  const source = record(record(run?.budget).limits);
  const budget = text.trim() ? jsonObject(text, '共同预算') : { ...source };
  if (!Object.keys(budget).length) throw new Error('来源尚无可复制预算，请等待来源核对或提供完整预算');
  for (const [name, value] of Object.entries(budget)) {
    if (name === 'max_cost_micro_usd' && value === null) continue;
    if (integer(value) === undefined) throw new Error(`共同预算 ${name} 须为非负安全整数`);
  }
  if (run?.model_external === true || typeof run?.model === 'string' && run.model.startsWith('openai/')) {
    const cost = parseUsd(money);
    if (text.trim() && budget.max_cost_micro_usd !== undefined && budget.max_cost_micro_usd !== cost) throw new Error('共同预算 JSON 与 USD 上限不一致');
    budget.max_cost_micro_usd = cost;
  }
  return budget;
}
export function experimentRequest(input: {
  scope: ExperimentScope; validatorRef: string; repeats: string; run: JsonObject | null;
  budget: string; money: string; groups: [GroupDraft, GroupDraft];
  mode: 'live' | 'hybrid'; until: string; calls: string; miss: 'stop' | 'fixture';
}): JsonObject {
  if (!/^(?:[1-9]|10)$/.test(input.repeats)) throw new Error('每组重复次数须为 1–10');
  const request: JsonObject = {
    schema_version: 1, source_run_id: identifier(input.scope.sourceRunId, '来源 Run'),
    from_commit: digest(input.scope.fromCommit, '安全起点'), validator_ref: digest(input.validatorRef, '验收器'),
    repeats: Number(input.repeats), budget: experimentBudget(input.run, input.budget, input.money),
    groups: [groupRequest('A', input.groups[0]), groupRequest('B', input.groups[1])],
  };
  if (input.mode === 'hybrid') request.hybrid = {
    schema_version: 1, until_commit: digest(input.until, '录制范围终点'),
    calls: identifiers(input.calls, '选定只读调用', 100), miss_policy: input.miss,
  };
  return request;
}

// 统计值使用十进制字符串；先检查长度，再以 BigInt 运算，避免精度丢失和异常长输入。
function big(value: Json | undefined): bigint | null {
  if (typeof value === 'number') return integer(value) === undefined ? null : BigInt(value);
  return typeof value === 'string' && /^(?:0|[1-9]\d{0,99})$/.test(value) ? BigInt(value) : null;
}
export function exact(value: Json | undefined, options: { places?: number; multiplier?: bigint; divisor?: bigint } = {}): string {
  if (value && typeof value === 'object' && (record(value).numerator === undefined || record(value).denominator === undefined)) return '未知';
  const fraction = record(value), n = big(fraction.numerator ?? value), d = fraction.denominator === undefined ? 1n : big(fraction.denominator);
  const places = options.places ?? 2;
  if (n === null || d === null || d === 0n || !Number.isInteger(places) || places < 0 || places > 12) return '未知';
  const denominator = d * (options.divisor ?? 1n), numerator = n * (options.multiplier ?? 1n);
  if (denominator <= 0n || numerator < 0n) return '未知';
  const scale = 10n ** BigInt(places), scaled = numerator * scale;
  const rounded = (scaled + denominator / 2n) / denominator;
  const decimal = String(rounded % scale).padStart(places, '0').replace(/0+$/, '');
  return `${scaled % denominator ? '≈' : ''}${(rounded / scale).toLocaleString('en-US')}${decimal ? '.' + decimal : ''}`;
}
export function rate(value: Json | undefined): string {
  const fraction = record(value), n = big(fraction.numerator), d = big(fraction.denominator);
  if (n === null || d === null || n > d) return '未知';
  const result = exact(value, { multiplier: 100n }); return result === '未知' ? result : result + '%';
}
export function money(value: Json | undefined): string { const result = exact(value, { places: 6, divisor: 1000000n }); return result === '未知' ? result : 'USD ' + result; }
export const count = (value: Json | undefined): string => integer(value) === undefined ? '未知' : String(value);
export const knownRejection = (code: string): boolean => ['validation', 'forbidden', 'unauthorized', 'not_found', 'unsupported_schema', 'plan_invalid', 'replay_mismatch', 'recording_missing', 'version_conflict'].includes(code);
