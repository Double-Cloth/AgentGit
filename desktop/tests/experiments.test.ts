import test from 'node:test';
import assert from 'node:assert/strict';
import { count, draft, eventId, exact, experimentBudget, experimentRequest, groupRequest, hasFeature, identifiers, knownRejection, money, rate } from '../src/shared/experiments';
import type { JsonObject } from '../src/shared/bridge';

const RUN = '00000000-0000-4000-8000-000000000001', OTHER = '00000000-0000-4000-8000-000000000002';
const scope = { sourceRunId: RUN, fromCommit: 'a'.repeat(64), fixtureSplit: 'screen' as const };
const run: JsonObject = { model: 'mock-research/v1', budget: { limits: { schema_version: 1, max_steps: 32, max_cost_micro_usd: null } } };

test('精确统计保留超安全整数、分数、微美元与近似标记，缺失值不变成零', () => {
  assert.equal(exact('900719925474099300000'), '900,719,925,474,099,300,000');
  assert.equal(exact({ numerator: '1', denominator: '8' }, { places: 3 }), '0.125');
  assert.equal(exact({ numerator: '1', denominator: '8' }), '≈0.13');
  assert.equal(exact({ numerator: '1', denominator: '3' }), '≈0.33');
  assert.equal(exact({ numerator: '0', denominator: '1' }), '0');
  assert.equal(exact({ numerator: '99999', denominator: '10000' }), '≈10');
  assert.equal(money('1'), 'USD 0.000001');
  assert.equal(money({ numerator: '1', denominator: '2' }), 'USD ≈0.000001');
  assert.equal(rate({ numerator: '2', denominator: '5' }), '40%');
  assert.equal(rate({ numerator: '3', denominator: '2' }), '未知');
  for (const value of [undefined, null, true, [], {}, { numerator: '1' }, { denominator: '2' }, { numerator: '1', denominator: '0' }, '-1', '01', '1.2', '1e3', '1'.repeat(101), 0.1, Number.MAX_SAFE_INTEGER + 1]) assert.equal(exact(value), '未知');
  assert.equal(exact('1', { places: 13 }), '未知'); assert.equal(count(null), '未知'); assert.equal(count(0), '0');
});

test('事件 ID 与调用 UUID 分离，拒绝不安全数字、重复与越界列表', () => {
  assert.equal(eventId('17'), 17); assert.equal(eventId(String(Number.MAX_SAFE_INTEGER)), Number.MAX_SAFE_INTEGER);
  for (const value of ['', '0', '01', '-1', '1.5', '1e2', RUN, '9007199254740992']) assert.throws(() => eventId(value));
  assert.deepEqual(identifiers(`${RUN},\n${OTHER}`, '调用', 2), [RUN, OTHER]);
  for (const value of ['', RUN + ',' + RUN, RUN + ',' + OTHER]) assert.throws(() => identifiers(value, '调用', 1));
});

test('分组只序列化已选条件，不混入旧 Prompt、计划、权限或验收器', () => {
  const base = { ...draft('来源'), prompt: '旧 Prompt', plan: '{"schema_version":1}', ids: RUN };
  assert.deepEqual(groupRequest('A', base), { schema_version: 1, key: 'A', label: '来源' });
  assert.deepEqual(groupRequest('B', { ...base, kind: 'prompt' }), { schema_version: 1, key: 'B', label: '来源', prompt: '旧 Prompt' });
  assert.deepEqual(groupRequest('B', { ...base, kind: 'interventions' }).interventions, [RUN]);
  assert.throws(() => groupRequest('B', { ...base, kind: 'plan', plan: '[]' }));
  assert.throws(() => groupRequest('B', { ...base, kind: 'prompt', prompt: ' ' }));
  assert.throws(() => groupRequest('B', { ...base, label: ' ' }));
});

test('共同预算复制来源，外部模型必须显式费用且拒绝冲突，混合请求保持固定范围', () => {
  const budget = experimentBudget(run, '', ''); budget.max_steps = 1;
  assert.equal(((run.budget as JsonObject).limits as JsonObject).max_steps, 32);
  const external = { ...run, model_external: true };
  assert.throws(() => experimentBudget(external, '', ''));
  assert.equal(experimentBudget(external, '', '0.000001').max_cost_micro_usd, 1);
  assert.throws(() => experimentBudget(external, '{"max_cost_micro_usd":2}', '0.000001'));
  assert.throws(() => experimentBudget(run, '{"max_steps":1.5}', ''));
  const input = { scope, validatorRef: 'b'.repeat(64), repeats: '5', run, budget: '', money: '', groups: [draft('A'), draft('B')] as [ReturnType<typeof draft>, ReturnType<typeof draft>], mode: 'live' as const, until: 'c'.repeat(64), calls: OTHER, miss: 'stop' as const };
  const request = experimentRequest(input); assert.equal(request.repeats, 5); assert.equal(request.hybrid, undefined);
  assert.deepEqual(experimentRequest({ ...input, mode: 'hybrid' }).hybrid, { schema_version: 1, until_commit: 'c'.repeat(64), calls: [OTHER], miss_policy: 'stop' });
  for (const repeats of ['0', '11', '1.5', '05', '']) assert.throws(() => experimentRequest({ ...input, repeats }));
});

test('只把明确拒绝视为可重新定义，超时和响应解析错误仍需原键确认', () => {
  for (const code of ['validation', 'version_conflict', 'unsupported_schema']) assert.equal(knownRejection(code), true);
  for (const code of ['timeout', 'invalid_response', 'output_limit', 'internal', 'request_cancelled']) assert.equal(knownRejection(code), false);
  assert.equal(hasFeature({ features: ['controlled_experiments'] }, 'controlled_experiments'), true);
  assert.equal(hasFeature(null, 'controlled_experiments'), false);
});
