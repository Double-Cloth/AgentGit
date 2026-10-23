import React, { useState } from 'react';
import type { JsonObject } from '../shared/bridge';
import { count, exact, money, rate } from '../shared/experiments';
import { JsonView, object, rows, str } from './ui';

const METRICS = [
  ['actual.model_calls', '实际模型调用', '次'], ['actual.tool_calls', '实际工具调用', '次'],
  ['actual.wall_ms', '墙钟时延', 'ms'], ['mock.total_bytes', 'Mock 用量', '字节'],
  ['provider.input_tokens', '供应商输入', 'Token'], ['provider.output_tokens', '供应商输出', 'Token'],
  ['provider.estimated_cost_micro_usd', '价格表估价', 'USD'], ['provider.reserved_cost_micro_usd', '费用预留', 'USD'],
  ['provider.billed_cost_micro_usd', '供应商账单', 'USD'],
] as const;

export function ExperimentStatistics({ statistics }: { statistics: JsonObject }) {
  const [scope, setScope] = useState<'consumption_to_date' | 'assessed_trials'>('consumption_to_date');
  const groups = rows(statistics.groups);
  if (!groups.length) return <p className="muted">当前报告没有可用统计；不会将缺失指标显示为零。</p>;
  return <section className="experiment-statistics" aria-labelledby="experiment-statistics-title">
    <div className="section-heading"><h2 id="experiment-statistics-title">分组结果与用量</h2><label className="compact-label">统计范围<select value={scope} onChange={event => setScope(event.target.value as typeof scope)}>
      <option value="consumption_to_date">截至当前的全部消耗</option><option value="assessed_trials">已验收的完整试验</option>
    </select></label></div>
    <p className="small muted">通过率包含全部预定 trial；未完成不算通过。均值与样本方差只使用已知值，单样本方差未知。固定语料的重复结果不证明真实模型稳定或严格因果。</p>
    <div className="experiment-group-results">{groups.map(group => {
      const selected = object(group[scope]), metrics = object(selected.metrics), hard = object(group.hard_assertions);
      const all = object(object(group.consumption_to_date).metrics);
      return <section key={str(group.key)} aria-label={`${str(group.key)} 组统计`}>
        <h3>{str(group.key)} 组 · 通过 {count(group.passed)} / {count(group.planned_trials)}（{rate(group.success_rate)}）</h3>
        <p>未通过 {count(group.failed)} · 未完成 {count(group.incomplete)} · 整体验收未评估 {count(hard.not_evaluated)}</p>
        <p className="small muted">已知估价小计 {money(object(all['provider.known_estimated_cost_micro_usd']).sum)}；费用预留 {money(object(all['provider.reserved_cost_micro_usd']).sum)}。这是模型价格表估计，不是供应商账单；本地 Mock 不收模型费用。</p>
        {selected.trial_count === 0 ? <p className="muted">此范围尚无完整试验，均值和方差未知。</p> : <div className="table-scroll"><table><caption className="sr-only">{str(group.key)} 组的指标、样本覆盖、均值和样本方差</caption>
          <thead><tr><th scope="col">指标</th><th scope="col">已知 / 范围</th><th scope="col">缺失 / 不适用</th><th scope="col">均值</th><th scope="col">样本方差</th></tr></thead>
          <tbody>{METRICS.map(([name, title, unit]) => {
            const metric = object(metrics[name]), monetary = unit === 'USD';
            const mean = monetary ? money(metric.mean) : exact(metric.mean);
            const variance = exact(metric.sample_variance, monetary ? { places: 12, divisor: 1000000000000n } : {});
            return <tr key={name}><th scope="row">{title}<span className="small muted">{monetary ? '' : `（${unit}）`}</span></th>
              <td>{count(metric.samples)} / {count(selected.trial_count)}</td><td>{count(metric.missing_samples)} / {count(metric.not_applicable_samples)}</td>
              <td>{mean}</td><td>{variance}{variance === '未知' ? '' : ` ${unit}²`}</td></tr>;
          })}</tbody></table></div>}
        <p className="small muted">恢复 / 再次领取 {count(object(group.recovery).trials)} 项，其中通过 {count(object(group.recovery).passed)}。未列出的细分规则可能没有执行，不能据此判断通过。</p>
      </section>;
    })}</div>
    <details><summary>查看精确统计与限制原文</summary><JsonView value={statistics} /></details>
  </section>;
}
