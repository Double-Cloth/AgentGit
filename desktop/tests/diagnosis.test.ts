import test from 'node:test';
import assert from 'node:assert/strict';
import type { JsonObject } from '../src/shared/bridge';
import { candidatePatch, candidatePreset, evidenceTarget, matchesEvidence } from '../src/shared/diagnosis';

const RUN = '00000000-0000-4000-8000-000000000001';

test('证据跳转保留 Run、seq 与数据库 ID，旧事件不受展示窗口截断', () => {
  const target = evidenceTarget({ run_id: RUN, seq: 4321, event_id: 9001 });
  assert.deepEqual(target, { runId: RUN, seq: 4321, eventId: 9001 });
  assert.ok(matchesEvidence({ run_id: RUN, seq: 4321, id: 9001 }, target!));
  for (const row of [{ run_id: RUN, seq: 4321, id: 9002 }, { run_id: RUN, seq: 4322, id: 9001 }, { run_id: RUN.replace('0001', '0002'), seq: 4321, id: 9001 }]) assert.equal(matchesEvidence(row, target!), false);
});

test('缺失、非安全整数、布尔、路径或伪 UUID 不能成为可点击证据', () => {
  const valid: JsonObject = { run_id: RUN, seq: 2, event_id: 3 };
  for (const change of [{ event_id: null }, { event_id: 0 }, { event_id: true }, { event_id: '3' }, { event_id: Number.MAX_SAFE_INTEGER + 1 }, { seq: 1.5 }, { seq: -1 }, { run_id: '../private' }, { run_id: '-'.repeat(36) }]) assert.equal(evidenceTarget({ ...valid, ...change }), null);
});

test('候选干预沿用核验前置条件，A/B 交接保留原验收器及 confirm 划分', () => {
  const context: JsonObject = { source_run_id: RUN, base_commit: 'a'.repeat(64), target_commit_id: 'a'.repeat(64), target_event_id: 55, precondition: { validator_ref: 'b'.repeat(64) } };
  const patch = candidatePatch(context, { event: { run_id: RUN, seq: 9, event_id: 90 } }, 'PLAN_PATCH', JSON.stringify({ nodes: [] }), '保留已完成计划节点');
  assert.equal(patch.precondition, context.precondition); assert.deepEqual(patch.payload, { kind: 'PLAN_PATCH', plan: { nodes: [] } });
  const preset = candidatePreset(context, { validator_ref: 'b'.repeat(64), manifest: { fixture_split: 'confirm' } }, RUN);
  assert.equal(preset.fixtureSplit, 'confirm'); assert.equal(preset.validatorRef, 'b'.repeat(64));
  assert.throws(() => candidatePreset(context, { validator_ref: 'c'.repeat(64), manifest: { fixture_split: 'confirm' } }, RUN));
  assert.throws(() => candidatePatch(context, { event: { run_id: RUN, seq: 9, event_id: 90 } }, 'PROMPT_PATCH', '', '不能创建空 Prompt'));
});
