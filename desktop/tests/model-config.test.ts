import test from 'node:test';
import assert from 'node:assert/strict';
import { buildRunConfig, integer, jsonObject, liveBudget, liveIsExternal, modelChoices, parseUsd, usd } from '../src/shared/model-config';
import type { JsonObject } from '../src/shared/bridge';

const base: JsonObject = { model: 'mock-research/v1', tools: ['corpus_search'], limits: { schema_version: 1, max_steps: 32 } };
const capabilities: JsonObject = { default_config: base, models: [
  { id: 'mock-research/v1', title: 'Mock', external: false, config_patch: { model: 'mock-research/v1' } },
  { id: 'openai/fixed', title: '固定供应商', external: true, currency: 'USD', config_patch: { model: 'openai/fixed', openai: { model: 'fixed' } }, minimum_reservation: { cost_micro_usd: 100 } },
] };

test('USD 解析与显示不经过浮点乘除；保留单个 micro-USD 和最大安全整数', () => {
  assert.equal(parseUsd('0.000001'), 1);
  assert.equal(parseUsd('1.234567'), 1_234_567);
  assert.equal(parseUsd('9007199254.740991'), Number.MAX_SAFE_INTEGER);
  assert.equal(usd(Number.MAX_SAFE_INTEGER), 'USD 9,007,199,254.740991');
  assert.equal(usd(1), 'USD 0.000001');
  assert.equal(usd(0), 'USD 0.000000');
  for (const value of ['', '0', '-1', 'NaN', '1e6', '1,00', '.25', '1.0000001', '9007199254.740992', '1'.repeat(1000)]) assert.throws(() => parseUsd(value));
  for (const value of [null, -1, 0.2, Number.MAX_SAFE_INTEGER + 1]) assert.equal(usd(value), '未知');
  assert.equal(integer(undefined), undefined);
});
test('旧后端只提供离线模型；显式未知币种不会成为可用模型', () => {
  assert.equal(modelChoices({ default_config: base }).length, 1);
  assert.equal(modelChoices(null).length, 0);
  assert.equal(modelChoices({ models: [{ id: 'other', title: '其他', external: true, currency: 'EUR', config_patch: { model: 'other' } }] }).length, 0);
});
test('外部模型必须输入费用；合并预算不修改能力模板或静默覆盖冲突', () => {
  const choice = modelChoices(capabilities)[1]!;
  assert.throws(() => buildRunConfig(base, choice, '', ''));
  const result = buildRunConfig(base, choice, '', '0.25');
  assert.equal(result.model, 'openai/fixed');
  assert.equal((result.limits as JsonObject).max_cost_micro_usd, 250_000);
  assert.equal((base.limits as JsonObject).max_cost_micro_usd, undefined);
  assert.throws(() => buildRunConfig(base, choice, '{"max_cost_micro_usd":100}', '1'));
  assert.throws(() => buildRunConfig(base, choice, '{"max_steps":0.5}', '1'));
  assert.throws(() => buildRunConfig(base, choice, '[]', '1'));
  assert.throws(() => jsonObject('{', '预算'));
});
test('Mock 切换删除供应商选项；旧默认配置序列化保持不变', () => {
  const choice = modelChoices(capabilities)[0]!;
  assert.deepEqual(buildRunConfig(base, choice, '', ''), base);
  assert.equal(buildRunConfig({ ...base, openai: {} }, choice, '', '').openai, undefined);
});
test('Live 的 null 配置沿用来源，完整 Mock 配置可显式切换', () => {
  const run = { model_external: true, budget: { limits: { max_steps: 32, max_cost_micro_usd: 1_000_000 } } };
  assert.equal(liveIsExternal(run, '{}'), true);
  assert.equal(liveIsExternal(run, '{"config":null}'), true);
  assert.equal(liveIsExternal(run, '{"config":{}}'), false);
  assert.equal(liveIsExternal(run, '{"config":{"model":"mock-research/v1"}}'), false);
  assert.equal(liveIsExternal({ model_external: false }, '{"config":{"model":"openai/fixed"}}'), true);
  assert.deepEqual(liveBudget(run, { config: null }, '', '0.5'), { max_steps: 32, max_cost_micro_usd: 500_000 });
  assert.throws(() => liveBudget(run, {}, '{"max_cost_micro_usd":1}', '1'));
  assert.throws(() => liveBudget(run, {}, '', '0'));
});
