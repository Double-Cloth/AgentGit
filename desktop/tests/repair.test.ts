import test from 'node:test';
import assert from 'node:assert/strict';
import { preparedRepair, repairReviewGate } from '../src/shared/repair';
import type { JsonObject } from '../src/shared/bridge';

const uuid = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
const draft = () => ({ run: uuid(1), base: 'a'.repeat(64), validator: 'b'.repeat(64), targets: '[{"event_id":9100}]', regression: [2, 3, 4, 5].map(i => ({ run: uuid(i), commit: 'c'.repeat(64) })), budget: '', hybrid: '', external: false, stochastic: false });

test('修复准备冻结四项来源和原事件，不猜测目标或放宽真实模型预算', () => {
  const input = draft(), result = preparedRepair(input);
  assert.equal((result.search as JsonObject).failed_run_id, uuid(1));
  assert.deepEqual((result.search as JsonObject).targets, [{ schema_version: 1, event_id: 9100 }]);
  for (const change of [{ targets: '[]' }, { targets: '[{"event_id":true}]' }, { targets: '[{"event_id":9007199254740992}]' }, { targets: '[{"event_id":1},{"event_id":1}]' }, { regression: [input.regression[0]!, input.regression[0]!, input.regression[0]!, input.regression[0]!] }, { external: true }, { external: true, stochastic: true, budget: '{}' }]) assert.throws(() => preparedRepair({ ...input, ...change }));
  assert.equal((preparedRepair({ ...input, stochastic: true }).search as JsonObject).fixture_scope, 'stochastic_fixture');
});

test('精确票据、版本、封存门槛和观测范围共同控制修复采纳，失败仍可拒绝', () => {
  const view: JsonObject = { proposal_id: uuid(1), proposal_ref: 'a'.repeat(64), current_config_version: 29, review: null,
    proposal: { source_config_id: uuid(2), source_config_version: 1, after: {}, adoption_scope: 'create_config_version', verification_ref: 'b'.repeat(64), verification: { status: 'verified_under_fixture', confirm_determinate: 6, confirm_passed: 6, regression_passed: 4, production_eligible: true, has_observation_patch: false, lock_ref: 'c'.repeat(64) } } };
  const session: JsonObject = { review_id: uuid(3), ticket: { proposal_id: uuid(1), proposal_ref: 'a'.repeat(64), source_config_id: uuid(2), source_config_version: 1, verification_ref: 'b'.repeat(64), lock_ref: 'c'.repeat(64), expected_config_version: 29 } };
  assert.equal(repairReviewGate(view, session).approve, true);
  assert.equal(repairReviewGate({ ...view, current_config_version: 30 }, session).approve, false);
  assert.equal(repairReviewGate({ ...view, review: {} }, session).reject, false);
  const proposal = view.proposal as JsonObject, verification = proposal.verification as JsonObject;
  const observation = { ...view, proposal: { ...proposal, after: null, adoption_scope: 'save_experiment_only', verification: { ...verification, has_observation_patch: true, production_eligible: false } } };
  const gate = repairReviewGate(observation, session); assert.equal(gate.approve, false); assert.equal(gate.save, true);
  const failed = repairReviewGate({ ...view, proposal: { ...proposal, verification: { ...verification, status: 'rejected', confirm_passed: 0 } } }, session);
  assert.equal(failed.approve, false); assert.equal(failed.save, false); assert.equal(failed.reject, true);
  for (const key of ['proposal_ref', 'source_config_id', 'verification_ref', 'lock_ref']) assert.equal(repairReviewGate(view, { ...session, ticket: { ...(session.ticket as JsonObject), [key]: 'changed' } }).approve, false);
});
