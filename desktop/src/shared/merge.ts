import type { Json, JsonObject, MergeVersion } from './bridge';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i, DIGEST = /^[0-9a-f]{64}$/;
const object = (value: Json | undefined): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const digest = (value: Json | undefined) => typeof value === 'string' && DIGEST.test(value);
export function mergeVersion(view: JsonObject, key: string): MergeVersion {
  const revision = object(view.revision);
  if (typeof view.proposal_id !== 'string' || !UUID.test(view.proposal_id) || typeof revision.revision !== 'number'
      || !Number.isSafeInteger(revision.revision) || revision.revision < 1 || revision.revision > 100 || !digest(revision.revision_ref)) throw new Error('当前提案的确切修订无效，请重新读取');
  return { proposalId: view.proposal_id, expectedRevision: revision.revision, expectedRevisionRef: String(revision.revision_ref), key };
}
export function mergeReviewGate(view: JsonObject, session: JsonObject | null) {
  const revision = object(view.revision), candidate = object(view.candidate), validation = object(view.validation), result = object(validation.result);
  const ticket = object(session?.ticket), request = object(object(view.manifest).request);
  const samePin = (left: Json | undefined, right: Json | undefined) => {
    const a = object(left), b = object(right);
    return ['name', 'head', 'source_run_id', 'version'].every(key => a[key] !== undefined && a[key] === b[key])
      && digest(a.head) && typeof a.source_run_id === 'string' && UUID.test(a.source_run_id)
      && typeof a.version === 'number' && Number.isSafeInteger(a.version) && a.version >= 0;
  };
  const validated = view.status === 'validated' && view.approval === null && view.rejection === null && result.validated === true
    && object(result.report_evaluation).passed === true && Array.isArray(result.conflicts) && result.conflicts.length === 0
    && Array.isArray(candidate.conflicts) && candidate.conflicts.length === 0 && candidate.state !== null && typeof candidate.state === 'object' && !Array.isArray(candidate.state)
    && typeof revision.revision === 'number' && Number.isSafeInteger(revision.revision) && revision.revision >= 1 && revision.revision <= 100 && digest(revision.revision_ref)
    && typeof view.validation_id === 'string' && UUID.test(view.validation_id)
    && digest(candidate.candidate_digest) && candidate.candidate_digest === revision.candidate_digest && candidate.candidate_digest === result.candidate_digest
    && digest(view.proposal_ref) && validation.proposal_ref === view.proposal_ref && validation.revision_ref === revision.revision_ref;
  const exact = Boolean(session && session.proposal_id === view.proposal_id && session.current === true
    && typeof session.review_id === 'string' && UUID.test(session.review_id) && digest(session.ticket_ref)
    && digest(view.validation_ref) && ticket.proposal_ref === view.proposal_ref && ticket.revision_ref === revision.revision_ref
    && ticket.validation_id === view.validation_id && ticket.validation_ref === view.validation_ref && ticket.candidate_digest === candidate.candidate_digest
    && ticket.execution === 'not_started' && samePin(ticket.ours, request.ours) && samePin(ticket.theirs, request.theirs));
  return { validated, exact, approve: validated && exact };
}

export const MERGE_STATUS: Record<string, string> = { open: '未关闭（待核对）', draft: '候选待验证', needs_review: '存在阻断项', validated: '验证通过，待本人审阅', stale: '来源已变化', merged: '已合并，尚未执行', rejected: '已拒绝' };
