import type { Json, JsonObject } from './bridge';
import { UUID, digest, eventId, identifier } from './experiments';
import { jsonObject, record } from './model-config';

export type EvidenceTarget = { runId: string; seq: number; eventId: number };
export type CandidateExperimentPreset = { sourceRunId: string; fromCommit: string; validatorRef: string; fixtureSplit: 'screen' | 'confirm' | 'regression'; interventionId: string };

export function candidatePatch(context: JsonObject, candidate: JsonObject, operator: 'PROMPT_PATCH' | 'PLAN_PATCH', content: string, rationale: string): JsonObject {
  const evidence = evidenceTarget(candidate.event);
  if (!evidence || evidence.runId !== context.source_run_id || !rationale.trim() || rationale.length > 2000) throw new Error('候选引用或干预理由无效');
  if (operator === 'PROMPT_PATCH' && (!content.trim() || content.length > 16000)) throw new Error('完整 Prompt 须为 1–16000 字');
  return { schema_version: 1, source_run_id: identifier(String(context.source_run_id), '来源 Run'), base_commit: digest(String(context.base_commit), '安全起点'), target_commit_id: digest(String(context.target_commit_id), '目标检查点'), target_event_id: eventId(String(context.target_event_id)), precondition: record(context.precondition), operator,
    payload: operator === 'PLAN_PATCH' ? { kind: operator, plan: jsonObject(content, '完整计划') } : { kind: operator, prompt: content }, effect_policy: 'sandbox_or_approval', provenance: { rationale, evidence: [{ run_id: evidence.runId, seq: evidence.seq }] } };
}

export function candidatePreset(context: JsonObject, validator: JsonObject, interventionId: string): CandidateExperimentPreset {
  const validatorRef = digest(String(record(context.precondition).validator_ref), '原验收器'), split = record(validator.manifest).fixture_split;
  if (validator.validator_ref !== validatorRef || !['screen', 'confirm', 'regression'].includes(String(split))) throw new Error('验收器引用或样例划分无法核对');
  return { sourceRunId: identifier(String(context.source_run_id), '来源 Run'), fromCommit: digest(String(context.base_commit), '安全起点'), validatorRef, fixtureSplit: split as CandidateExperimentPreset['fixtureSplit'], interventionId: identifier(interventionId, '已创建干预') };
}

export function evidenceTarget(value: Json | undefined): EvidenceTarget | null {
  const ref = record(value);
  if (typeof ref.run_id !== 'string' || !UUID.test(ref.run_id)
      || typeof ref.seq !== 'number' || !Number.isSafeInteger(ref.seq) || ref.seq < 1
      || typeof ref.event_id !== 'number' || !Number.isSafeInteger(ref.event_id) || ref.event_id < 1) return null;
  return { runId: ref.run_id, seq: ref.seq, eventId: ref.event_id };
}

export function matchesEvidence(value: Json | undefined, target: EvidenceTarget): boolean {
  const row = record(value);
  return row.run_id === target.runId && row.seq === target.seq && row.id === target.eventId;
}
