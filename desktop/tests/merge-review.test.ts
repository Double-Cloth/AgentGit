import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mergeReviewGate, mergeVersion } from '../src/shared/merge';
import type { JsonObject } from '../src/shared/bridge';

const ID = '00000000-0000-4000-8000-000000000001', A = 'a'.repeat(64), B = 'b'.repeat(64), C = 'c'.repeat(64);
function fixture() {
  const ours = { name: 'ours', head: A, source_run_id: ID, version: 3 }, theirs = { name: 'theirs', head: B, source_run_id: ID, version: 4 };
  const view: JsonObject = { proposal_id: ID, proposal_ref: A, status: 'validated', approval: null, rejection: null,
    revision: { revision: 2, revision_ref: B, candidate_digest: C }, candidate: { candidate_digest: C, state: {}, conflicts: [] },
    validation_id: ID, validation_ref: C, validation: { proposal_ref: A, revision_ref: B, result: { candidate_digest: C, validated: true, report_evaluation: { passed: true }, conflicts: [] } }, manifest: { request: { ours, theirs } } };
  const session: JsonObject = { proposal_id: ID, review_id: ID, ticket_ref: B, current: true, ticket: { proposal_ref: A, revision_ref: B, validation_id: ID, validation_ref: C, candidate_digest: C, ours, theirs, execution: 'not_started' } };
  return { view, session };
}
test('S11 仅本次已核对票据、当前来源及完整硬验证同时匹配时允许批准', () => {
  const { view, session } = fixture();
  assert.deepEqual(mergeReviewGate(view, session), { validated: true, exact: true, approve: true });
  assert.equal(mergeReviewGate(view, null).approve, false);
  for (const key of ['proposal_id', 'review_id', 'ticket_ref', 'current']) {
    const changed = structuredClone(session); delete changed[key]; assert.equal(mergeReviewGate(view, changed).approve, false, key);
  }
  for (const key of ['proposal_ref', 'revision_ref', 'validation_id', 'validation_ref', 'candidate_digest', 'ours', 'theirs', 'execution']) {
    const changed = structuredClone(session); delete (changed.ticket as JsonObject)[key]; assert.equal(mergeReviewGate(view, changed).approve, false, key);
  }
  for (const state of ['stale', 'merged', 'rejected', 'needs_review', 'draft']) assert.equal(mergeReviewGate({ ...view, status: state }, session).approve, false);
  const dirty = structuredClone(view); (dirty.candidate as JsonObject).candidate_digest = A; assert.equal(mergeReviewGate(dirty, session).approve, false);
  const failed = structuredClone(view); ((failed.validation as JsonObject).result as JsonObject).validated = false; assert.equal(mergeReviewGate(failed, session).approve, false);
  const conflict = structuredClone(view); (conflict.candidate as JsonObject).conflicts = [{ kind: 'EFFECT_CONFLICT' }]; assert.equal(mergeReviewGate(conflict, session).approve, false);
});
test('S11 请求版号不以默认值替代缺失，拒绝布尔、浮点和越界', () => {
  const { view } = fixture(); assert.deepEqual(mergeVersion(view, 'exact'), { proposalId: ID, expectedRevision: 2, expectedRevisionRef: B, key: 'exact' });
  for (const value of [null, false, 0, 101, 1.1, Number.MAX_SAFE_INTEGER + 1]) assert.throws(() => mergeVersion({ ...view, revision: { ...(view.revision as JsonObject), revision: value } }, 'exact'));
});
