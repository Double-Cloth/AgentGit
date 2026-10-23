import React, { useId } from 'react';
import type { Json, JsonObject } from '../shared/bridge';
import { integer, usd } from '../shared/model-config';
import { object, str } from './ui';

function count(value: Json | undefined): string { const n = integer(value); return n === undefined ? '未知' : n.toLocaleString('zh-CN'); }
function maximum(a: Json | undefined, b: Json | undefined): number | undefined {
  const left = integer(a), right = integer(b);
  return left === undefined && right === undefined ? undefined : Math.max(left ?? 0, right ?? 0);
}
export function ReplayUsage({ report }: { report: JsonObject }) {
  if (!report.mode) return null;
  const actual = object(report.actual), logical = object(report.logical_usage), meter = object(logical.model_meter);
  return <div className="replay-usage"><h3>{report.mode === 'recorded' ? 'Recorded 实际用量' : 'Live 实际用量'}</h3>
    <dl className="facts"><div><dt>模型尝试</dt><dd>{count(actual.model_calls)}</dd></div><div><dt>工具调用</dt><dd>{count(actual.tool_calls)}</dd></div>{(actual.currency === 'USD' || meter.currency === 'USD') && <div><dt>本次费用估计</dt><dd>{usd(actual.cost_micro_units)}</dd></div>}</dl>
    {report.mode === 'recorded' && <p className="small muted">历史逻辑用量保留在原文中；上方只显示本次回放的实际用量。历史验证通过不等于任务成功。</p>}
  </div>;
}
export default function BudgetSummary({ run }: { run: JsonObject }) {
  const id = useId(), budget = object(run.budget);
  if (!Object.keys(budget).length) return null;
  const usage = object(budget.usage), delta = object(budget.new_usage), totals = object(usage.model_meter);
  const baseline = object(object(budget.baseline).model_meter), pending = object(budget.pending_model_reservation);
  const remaining = object(budget.remaining), current = Object.keys(delta).length ? delta : usage;
  const recorded = run.mode === 'recorded', currency = budget.currency === 'USD' || totals.currency === 'USD';
  const measured = integer(totals.measured_calls), oldMeasured = integer(baseline.measured_calls) ?? 0;
  const estimates = integer(totals.estimated_calls), oldEstimates = integer(baseline.estimated_calls) ?? 0;
  const held = maximum(totals.reserved_cost_micro_usd, pending.cost_micro_usd);
  const hasDelta = Object.keys(delta).length > 0;
  const mock = run.model_external === false || run.model === 'mock-research/v1'
    || run.model_external === undefined && !run.model && usage.usage_kind === 'mock_utf8_bytes' && totals.currency !== 'USD';
  const unitLabel = hasDelta ? mock ? '本次新增 Mock 字节' : '本次新增 Token / 估计'
    : usage.usage_kind === 'mixed_units' ? '累计混合单位' : mock ? 'Mock 字节计量' : '已结算 Token / 估计';
  return <section className="budget-summary" aria-labelledby={id}><h3 id={id}>{recorded ? '历史逻辑预算与计量' : '预算与计量'}</h3>
    <p className="small muted">模型 <code>{str(run.model) || (currency ? '供应商模型（配置见原文）' : 'Mock · 离线')}</code>{run.model_external === true ? ' · 账户状态以本次结果为准' : ''}</p>
    <dl className="facts"><div><dt>{hasDelta ? '本次新增模型尝试' : '模型尝试'}</dt><dd>{count(current.model_calls)}</dd></div><div><dt>工具调用</dt><dd>{count(current.tool_calls)}</dd></div><div><dt>{unitLabel}</dt><dd>{count(current.tokens)}</dd></div><div><dt>{mock ? 'Mock 字节预留' : 'Token 预留'}</dt><dd>{count(maximum(usage.reserved_tokens, pending.tokens))}</dd></div>
      {currency && <><div><dt>{recorded ? '历史已结算估计' : '已结算费用估计'}</dt><dd>{usd(current.cost_micro_units)}</dd></div><div><dt>{recorded ? '历史未释放费用预留' : '未释放费用预留'}</dt><dd>{usd(held)}</dd></div><div><dt>未核价费用预留</dt><dd>{usd(totals.unpriced_cost_micro_usd)}</dd></div><div><dt>剩余费用预算</dt><dd>{usd(remaining.cost_micro_usd)}</dd></div></>}
    </dl>
    {Number(pending.call_count) > 0 && <p className="small muted">另有 {count(pending.call_count)} 次模型调用已受理、尚未观察结果，已保守预留。</p>}
    {currency && <p className="small muted">费用均为 USD 价格表估计，不是供应商账单。实测调用 {count(measured === undefined ? undefined : Math.max(0, measured - oldMeasured))}；保守估计调用 {count(estimates === undefined ? undefined : Math.max(0, estimates - oldEstimates))}。缺失计量不会记作零。</p>}
    {usage.usage_kind === 'mixed_units' && <p className="small muted">累计历史包含 Mock 字节与供应商 Token，不能直接比较为统一 Token。{Object.keys(delta).length ? '当前显示 baseline 后新增用量；完整累计值见预算原文。' : '当前为累计混合计量；各类明细见预算原文。'}</p>}
    {run.status === 'outcome_unknown' && <p className="meter-attention">结果未知，预留尚未释放；不会自动重发。请先核对原调用。</p>}
    {integer(totals.unpriced_cost_micro_usd) !== undefined && Number(totals.unpriced_cost_micro_usd) > 0 && <p className="meter-attention">费用不可核验，已停止并保留预留；已结算估计不代表实际费用为零。</p>}
    <ReplayUsage report={object(run.replay)} />
  </section>;
}
