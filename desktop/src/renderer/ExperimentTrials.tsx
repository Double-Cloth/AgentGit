import React, { useRef, useState } from 'react';
import type { Inputs, JsonObject } from '../shared/bridge';
import { count, identifier, knownRejection, money } from '../shared/experiments';
import { integer } from '../shared/model-config';
import { call, JsonView, Notice, object, short, Status, str, useAction } from './ui';

type Control = Inputs['controlRun'] & { jobId: string };
const ACTIONS = { pause: '暂停', resume: '恢复', cancel: '取消' } as const;
const ALLOWED = { pause: ['queued', 'planning', 'running'], resume: ['paused'], cancel: ['queued', 'planning', 'running', 'paused', 'waiting_approval', 'outcome_unknown'] };

export function ExperimentTrials({ trials, refresh, openTimeline }: { trials: JsonObject[]; refresh: () => void; openTimeline: (id: string) => void }) {
  const action = useAction(), attempt = useRef<Control | null>(null), [pending, setPending] = useState<Control | null>(null);
  const [receipts, setReceipts] = useState<Record<string, Control['action']>>({});
  function control(trial: JsonObject, operation: Control['action']) {
    void action.run(async () => {
      if (!attempt.current) {
        const runId = identifier(str(trial.run_id), '当前 Run'), state = await call(window.agentgit.getRun({ runId }));
        const version = integer(state.state_version);
        if (str(state.id) !== runId || version === undefined) throw new Error('运行版本无法核对，请刷新实验后再操作');
        if (state.control_status === 'requested') throw new Error('已有控制请求等待 Worker 到达安全点，请等待真实状态刷新');
        if (!ALLOWED[operation].includes(str(state.status))) throw new Error('运行状态已变化，请刷新实验后再操作');
        attempt.current = { jobId: str(trial.job_id), runId, action: operation, expectedVersion: version, key: crypto.randomUUID() };
        setPending(attempt.current);
      }
      const { jobId: _, ...request } = attempt.current;
      const reply = await window.agentgit.controlRun(request);
      if (!reply.ok) {
        if (knownRejection(reply.error.code)) { attempt.current = null; setPending(null); }
        throw new Error(`${reply.error.message}（${reply.error.code}）`);
      }
      const accepted = attempt.current;
      setReceipts(previous => ({ ...previous, [accepted.jobId]: accepted.action }));
      attempt.current = null; setPending(null); refresh();
    });
  }
  return <section aria-labelledby="experiment-trials-title"><div className="section-heading"><h2 id="experiment-trials-title">独立试验</h2><button onClick={refresh}>刷新试验状态</button></div>
    <Notice>{action.error}</Notice>{pending && <p role="status">上次{ACTIONS[pending.action]}结果尚未确认，保留原请求和幂等键。<button disabled={action.busy} onClick={() => control({ job_id: pending.jobId }, pending.action)}>继续原{ACTIONS[pending.action]}请求</button></p>}
    <div className="table-scroll"><table><caption className="sr-only">各 trial 的运行、判定、实际调用、费用和控制</caption><thead><tr>
      <th scope="col">分组 / 次数</th><th scope="col">当前运行</th><th scope="col">状态 / 验收</th><th scope="col">实际调用 / 观测</th><th scope="col">模型费用</th><th scope="col">操作</th>
    </tr></thead><tbody>{trials.map(trial => {
      const replay = object(trial.replay), actual = object(replay.actual), measurement = object(actual.model_usage), provider = object(measurement.provider), mock = object(measurement.mock);
      const number = integer(trial.index), label = `${str(trial.group)} · ${number === undefined ? '未知' : number + 1}`;
      const verdict = trial.verdict === 'passed' ? '通过' : trial.verdict === 'failed' ? '未通过' : '未完成';
      const receipt = receipts[str(trial.job_id)], status = str(trial.status);
      const awaiting = receipt && (receipt === 'pause' ? ALLOWED.pause.includes(status) : receipt === 'resume' ? status === 'paused' : ALLOWED.cancel.includes(status));
      return <tr key={str(trial.job_id)}><th scope="row">{label}</th><td><code title={str(trial.run_id)}>{short(trial.run_id)}</code><br /><span className="small muted">领取 {count(trial.attempt)} 次</span></td>
        <td><Status value={trial.status} /><br />{verdict}{awaiting && <p className="small" role="status">已请求{ACTIONS[receipt]}，等待 Worker 安全点与真实状态刷新。</p>}{trial.status === 'outcome_unknown' && <p className="small">未知结果须先核对，不自动重发。</p>}</td>
        <td>模型 {count(actual.model_calls)} · 工具 {count(actual.tool_calls)}<br /><span className="small muted">{replay.mode === 'live' ? 'Live · 独立执行' : <>录制 {count(actual.recorded_observations)} · 模拟 {count(actual.fixture_calls)} · 合成 {count(actual.synthetic_observations)} · 未命中 {count(actual.hybrid_misses)}</>}</span>{(integer(actual.synthetic_observations) ?? 0) > 0 && <p className="small">synthetic observation · test_only</p>}</td>
        <td>{integer(provider.calls) ? <>估价 {money(provider.estimated_cost_micro_usd)}<br /><span className="small muted">预留 {money(provider.reserved_cost_micro_usd)}</span></> : integer(mock.calls) ? '本地 Mock · 无模型费用' : integer(provider.calls) === 0 && integer(mock.calls) === 0 ? '尚无模型调用' : '模型费用未知'}</td>
        <td><div className="button-group"><button onClick={() => openTimeline(str(trial.run_id))} aria-label={`打开 ${label} 时间线`}>时间线</button>{(Object.keys(ACTIONS) as Control['action'][]).map(operation => <button key={operation} aria-label={`${ACTIONS[operation]} ${label}`} disabled={action.busy || pending !== null || !ALLOWED[operation].includes(str(trial.status))} onClick={() => control(trial, operation)}>{ACTIONS[operation]}</button>)}</div>
          <details><summary>{label} 明细与来源</summary><dl className="facts"><div><dt>Job</dt><dd><code>{str(trial.job_id)}</code></dd></div><div><dt>初始 Run</dt><dd><code>{str(trial.initial_run_id)}</code></dd></div></dl><JsonView value={trial} /></details></td>
      </tr>;
    })}</tbody></table></div>
    <p className="small muted">控制使用点击时核对的当前 Run 版本；未知提交结果只可用原键继续。取消等待不等于已核对未知副作用。</p>
  </section>;
}
