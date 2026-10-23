import React, { useRef, useState } from 'react';
import type { Inputs, JsonObject } from '../shared/bridge';
import { digest, eventId, identifier, knownRejection, type ExperimentScope } from '../shared/experiments';
import { integer } from '../shared/model-config';
import { call, JsonView, Notice, object, rows, str, useAction, useRead } from './ui';

export function InterventionContext(props: {
  scope: ExperimentScope; validatorRef: string; active: boolean; disabled: boolean;
  onPending: (pending: boolean) => void; onPrompt: (prompt: string) => void;
  onCall: (id: string) => void; onIntervention: (id: string) => void;
}) {
  const [target, setTarget] = useState(''), [after, setAfter] = useState(0), [context, setContext] = useState<JsonObject | null>(null), load = useAction();
  const [replacement, setReplacement] = useState(''), [rationale, setRationale] = useState('仅用于受控试验，使用同项目已有完整只读录制替换目标观测。');
  const attempt = useRef<Inputs['createIntervention'] | null>(null), [locked, setLocked] = useState(false), [settled, setSettled] = useState(false), [created, setCreated] = useState(''), patch = useAction();
  const events = useRead(() => props.active ? call(window.agentgit.getEvents({ runId: props.scope.sourceRunId, afterSeq: after, limit: 100 })) : Promise.resolve({} as JsonObject), [after, props.active]);
  const readonly = ['tool:corpus_search/v1', 'tool:source_read/v1'].includes(str(object(object(context?.precondition).occurrence).provider));
  function createPatch() { void patch.run(async () => {
    if (!attempt.current) {
      if (!context || !readonly) throw new Error('请先读取受控只读工具的目标上下文');
      if (!rationale.trim() || rationale.length > 2000) throw new Error('干预理由须为 1–2000 字');
      const request: JsonObject = {
        schema_version: 1, source_run_id: props.scope.sourceRunId, base_commit: props.scope.fromCommit,
        target_commit_id: digest(str(context.target_commit_id), '目标检查点'), target_event_id: eventId(String(context.target_event_id)),
        operator: 'TOOL_RESPONSE_PATCH', payload: { kind: 'TOOL_RESPONSE_PATCH', replacement_call_id: identifier(replacement, '替换调用'), test_only: true },
        precondition: object(context.precondition), effect_policy: 'test_only', provenance: { rationale },
      };
      attempt.current = { request, key: crypto.randomUUID() }; setLocked(true); props.onPending(true);
    }
    const reply = await window.agentgit.createIntervention(attempt.current);
    if (!reply.ok) {
      const rejected = knownRejection(reply.error.code); setSettled(rejected); props.onPending(!rejected);
      throw new Error(`${reply.error.message}（${reply.error.code}）`);
    }
    setCreated(identifier(str(reply.data.intervention_id), '干预 ID')); setSettled(true); props.onPending(false);
  }); }
  return <section className="panel intervention-context"><h2>只读调用与干预</h2><p className="small muted">目标事件 ID 是正整数；调用 ID 是 UUID。可浏览调用开始事件，也可填写范围内的事件 ID。后端重新核验目标在起点之后及录制完整性。</p>
    <fieldset disabled={props.disabled || locked || load.busy || !props.active}><legend>读取目标上下文</legend><label>目标事件 ID<input value={target} inputMode="numeric" list="experiment-events" onChange={event => { setTarget(event.target.value); setContext(null); }} /></label>
      <datalist id="experiment-events">{rows(events.data?.items).filter(event => ['tool.started', 'llm.requested', 'call.reused'].includes(str(event.type))).map(event => <option key={String(event.id)} value={String(event.id)}>{String(event.seq)} · {str(event.type)} · {str(event.logical_step_id)}</option>)}</datalist>
      <div className="button-group"><button disabled={load.busy} onClick={() => { void load.run(async () => {
        setContext(null); const id = eventId(target);
        const value = await call(window.agentgit.getInterventionContext({ sourceRunId: props.scope.sourceRunId, fromCommit: props.scope.fromCommit, targetEventId: id, validatorRef: props.validatorRef }));
        if (value.source_run_id !== props.scope.sourceRunId || value.base_commit !== props.scope.fromCommit || value.target_event_id !== id || object(value.precondition).validator_ref !== props.validatorRef) throw new Error('调用上下文与冻结来源无法核对');
        setContext(value);
      }); }}>读取调用上下文</button><button disabled={after === 0 || load.busy} onClick={() => setAfter(0)}>首批事件</button><button disabled={rows(events.data?.items).length < 100 || integer(events.data?.next_after_seq) === undefined || load.busy} onClick={() => setAfter(integer(events.data?.next_after_seq) ?? after)}>下一批事件</button></div>
    </fieldset><Notice>{events.error || load.error}</Notice>
    {context && <><p className="small">事件 {String(context.target_event_id)} · {str(object(object(context.precondition).occurrence).provider)}</p><details><summary>查看签名、前置条件与来源配置</summary><JsonView value={context} /></details>
      <div className="button-group"><button disabled={props.disabled || !props.active || !str(object(context.config).prompt)} onClick={() => props.onPrompt(str(object(context.config).prompt))}>复制来源 Prompt 到 B</button><button disabled={props.disabled || !props.active || !readonly} onClick={() => props.onCall(str(context.source_call_id))}>加入 Hybrid 只读调用</button></div>
      {readonly && <details><summary>高级：冻结 test_only 工具响应补丁</summary><p className="small muted">只可引用同项目完整、同参数和同签名的只读录制；不能上传任意响应。创建干预后须显式加入 B 组。</p><fieldset disabled={props.disabled || locked || !props.active}><legend>替换录制</legend><label>替换调用 UUID<input value={replacement} onChange={event => setReplacement(event.target.value)} spellCheck={false} /></label><label>干预理由<textarea rows={3} maxLength={2000} value={rationale} onChange={event => setRationale(event.target.value)} /></label></fieldset>
        <Notice>{patch.error}</Notice><div className="button-group"><button disabled={props.disabled || !props.active || patch.busy || (locked && settled)} onClick={createPatch}>{locked && !settled ? '用原键继续冻结补丁' : '冻结工具响应补丁'}</button>
          {created && <button disabled={props.disabled || !props.active || patch.busy} onClick={() => { void patch.run(async () => props.onIntervention(created)); }}>加入 B 组干预</button>}
          {locked && settled && <button disabled={patch.busy || props.disabled} onClick={() => { attempt.current = null; setLocked(false); setSettled(false); setCreated(''); patch.clearError(); }}>定义另一补丁</button>}</div>
        {created && <p>已保存干预 <code>{created}</code> · test_only</p>}{attempt.current && <details><summary>固定补丁请求与幂等键</summary><JsonView value={attempt.current} /></details>}
      </details>}
    </>}
  </section>;
}
