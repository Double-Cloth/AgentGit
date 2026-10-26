import type { Json, JsonObject } from './bridge';
import { DIGEST, digest, identifier, UUID } from './experiments';

const object = (value: Json | undefined): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
export type RegressionDraft = { run: string; commit: string };
export function repairObject(text: string, name: string): JsonObject {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name}必须是 JSON 对象`);
  return value as JsonObject;
}
export function preparedRepair(input: { run: string; base: string; validator: string; targets: string; regression: RegressionDraft[]; budget: string; hybrid: string; external: boolean; stochastic: boolean }): JsonObject {
  const targets: unknown = JSON.parse(input.targets);
  if (!Array.isArray(targets) || targets.length < 1 || targets.length > 8) throw new Error('请提供 1–8 个明确事件目标');
  const ids = new Set<number>();
  const checked = targets.map(value => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('事件目标必须是对象');
    const row = value as JsonObject, id = row.event_id;
    if (typeof id !== 'number' || !Number.isSafeInteger(id) || id <= 0 || ids.has(id)) throw new Error('事件 ID 必须是不重复的正整数');
    ids.add(id);
    if (row.replacement_call_ids !== undefined && (!Array.isArray(row.replacement_call_ids) || row.replacement_call_ids.some(item => typeof item !== 'string' || !UUID.test(item)))) throw new Error('替换录制必须是完整调用 UUID');
    return { schema_version: 1, ...row };
  });
  if (input.regression.length !== 4) throw new Error('需要四个回归来源');
  const regression = input.regression.map(row => ({ schema_version: 1, source_run_id: identifier(row.run, '回归 Run'), from_commit: digest(row.commit, '回归起点') }));
  if (new Set(regression.map(row => row.source_run_id)).size !== 4) throw new Error('四项回归必须来自不同 Run 和任务');
  if (input.external && !input.budget.trim()) throw new Error('外部模型必须显式提供来源授权内的完整预算和 USD 上限');
  if (input.external && input.stochastic) throw new Error('真实模型不能标为随机 Mock fixture');
  return { schema_version: 1, search: { schema_version: 1, failed_run_id: identifier(input.run, '失败 Run'), base_commit: digest(input.base, '模型决策前起点'), validator_ref: digest(input.validator, 'screen 验收器'), targets: checked,
    ...(input.budget.trim() ? { budget: repairObject(input.budget, '预算') } : {}), ...(input.hybrid.trim() ? { hybrid: repairObject(input.hybrid, 'Hybrid 条件') } : {}), ...(input.stochastic ? { fixture_scope: 'stochastic_fixture' } : {}) }, regression };
}
export function repairReviewGate(view: JsonObject, session: JsonObject | null) {
  const proposal = object(view.proposal), verification = object(proposal.verification), ticket = object(session?.ticket);
  const qualified = ['verified_under_fixture', 'empirically_confirmed'].includes(String(verification.status)) && verification.confirm_determinate === 6 && typeof verification.confirm_passed === 'number' && verification.confirm_passed >= 5 && verification.regression_passed === 4;
  const exact = Boolean(session && typeof session.review_id === 'string' && UUID.test(session.review_id)
    && typeof view.proposal_ref === 'string' && DIGEST.test(view.proposal_ref)
    && typeof proposal.verification_ref === 'string' && DIGEST.test(proposal.verification_ref)
    && typeof verification.lock_ref === 'string' && DIGEST.test(verification.lock_ref)
    && typeof proposal.source_config_id === 'string' && UUID.test(proposal.source_config_id)
    && typeof proposal.source_config_version === 'number' && Number.isSafeInteger(proposal.source_config_version) && proposal.source_config_version > 0
    && ticket.proposal_id === view.proposal_id && ticket.proposal_ref === view.proposal_ref
    && ticket.lock_ref === verification.lock_ref && ticket.verification_ref === proposal.verification_ref
    && ticket.source_config_id === proposal.source_config_id && ticket.source_config_version === proposal.source_config_version
    && typeof ticket.expected_config_version === 'number' && Number.isSafeInteger(ticket.expected_config_version) && ticket.expected_config_version > 0
    && ticket.expected_config_version === view.current_config_version);
  const observed = proposal.adoption_scope === 'save_experiment_only' || verification.has_observation_patch === true;
  const undecided = view.review === null;
  return { qualified, observed, exact, approve: qualified && exact && undecided && !observed && verification.production_eligible === true && proposal.after !== null && typeof proposal.after === 'object' && !Array.isArray(proposal.after),
    save: qualified && exact && undecided, reject: exact && undecided };
}
