import React, { useRef, useState } from 'react';
import type { Inputs, JsonObject } from '../shared/bridge';
import { integer } from '../shared/model-config';
import { call, JsonView, Notice, object, str, useAction } from './ui';

interface Attempt { fork: Inputs['createBranch']; program: JsonObject; replay: JsonObject; submitKey: string; request?: Inputs['submitA2A']; branch?: JsonObject }

export default function A2ARecorded({ run, openTimeline }: { run: JsonObject; openTimeline: (id: string) => void }) {
  const candidate = object(object(run.adapter).recording_candidate), eligible = run.mode === 'live' && run.status === 'completed' && Boolean(candidate.from_commit);
  const [name, setName] = useState(''), [pending, setPending] = useState<Attempt | null>(null), [receipt, setReceipt] = useState<JsonObject | null>(null);
  const attempt = useRef<Attempt | null>(null), action = useAction();
  async function proceed(saved: Attempt, confirm = false) {
    if (confirm) {
      const value = await call(window.agentgit.getA2ASubmission({ key: saved.submitKey })); setReceipt(value); return;
    }
    if (!saved.branch) saved.branch = await call(window.agentgit.createBranch(saved.fork));
    if (!saved.request) {
      const version = integer(saved.branch.version);
      if (version === undefined || saved.branch.head_commit_id !== saved.replay.from_commit) throw new Error('Fork收据未包含确切版本／HEAD，请核对原分支，不改键另建');
      saved.request = { key: saved.submitKey, request: { schema_version: 1, program: saved.program, replay: saved.replay, branch_name: saved.fork.branchName, expected_head: str(saved.branch.head_commit_id), expected_version: version } };
    }
    const request = saved.request;
    if (!request) throw new Error('缺少固定Recorded请求，请核对原提交');
    const result = await call(window.agentgit.submitA2A(request)); setReceipt(result);
  }
  return <section className="a2a-recorded" aria-labelledby="a2a-recorded-title"><h3 id="a2a-recorded-title">A2A 成功轨迹 Recorded</h3><p>显式从完整成功Live的初始化点建立独立Fork，再提交同一固定程序的历史重算。来源在提交时重新严格核验；缺失录制失败，不回退Live。</p>
    {!eligible && <p role="status">当前来源未提供可核验候选。仅完整、同环境且无控制插入的本人成功Live可用于此回放；详情见适配器原文。</p>}
    <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
      if (!attempt.current) {
        if (!eligible || !name.trim()) throw new Error('请选择完整成功来源并填写独立分支名');
        const program = await call(window.agentgit.getA2AProgram({ runId: str(run.id) }));
        attempt.current = { program, replay: candidate, fork: { sourceRunId: str(run.id), fromCommit: str(candidate.from_commit), branchName: name.trim(), key: crypto.randomUUID() }, submitKey: crypto.randomUUID() };
        setPending(attempt.current);
      }
      await proceed(attempt.current);
    }); }}><label>Recorded 新分支名称<input value={name} maxLength={100} disabled={!eligible || Boolean(pending) || action.busy} onChange={event => setName(event.target.value)} /></label>
      {!pending && <button className="primary" disabled={!eligible || !name.trim() || action.busy}>建立 Fork 并提交 A2A Recorded</button>}
      {pending && !receipt && <div className="meter-attention"><p>分步请求已固定。查询只确认Recorded提交；重试继续原Fork及原提交，不新建键。</p><p className="small">Fork键 <code>{pending.fork.key}</code> · 提交原键 <code>{pending.submitKey}</code></p><div className="button-group"><button type="button" disabled={action.busy} onClick={() => void action.run(() => proceed(pending, true))}>只读确认 Recorded 原提交</button><button type="button" disabled={action.busy} onClick={() => void action.run(() => proceed(pending))}>继续原 Fork／Recorded 请求</button></div></div>}
      <Notice>{action.error}</Notice>{receipt && <div role="status"><p>Recorded已受理。本次取消仅停止本地回放。</p><button type="button" onClick={() => openTimeline(str(receipt.run_id))}>打开 A2A Recorded 时间线</button><JsonView value={receipt} /></div>}
    </form>
  </section>;
}
