import React, { useRef, useState } from 'react';
import type { Inputs, JsonObject } from '../shared/bridge';
import { call, Empty, JsonView, Notice, object, rows, short, Status, str, useAction, useRead, type PageProps } from './ui';

export const ROLE_NAMES: Record<string, string> = { supervisor: 'Supervisor · 协调', researcher: 'Researcher · 取证', verifier: 'Verifier · 核验', writer: 'Writer · 撰写' };
export function count(value: unknown): string { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '未知'; }

export default function Collaboration(props: PageProps & { active: boolean }) {
  const [after, setAfter] = useState(''), [lookup, setLookup] = useState(''), [creating, setCreating] = useState(false);
  const [localId, setLocalId] = useState('');
  const groupId = props.collaborationId || localId;
  const capabilities = useRead(() => call(window.agentgit.capabilities()), []);
  const supported = Array.isArray(capabilities.data?.features) && capabilities.data.features.includes('collaboration');
  const list = useRead(() => supported && props.active ? call(window.agentgit.listCollaborations({ ...(after ? { afterId: after } : {}), limit: 20 })) : Promise.resolve({} as JsonObject), [supported, props.active, after], props.active && supported ? 3000 : 0, `groups:${after}`, props.active);
  function open(id: string) { setLocalId(id); props.openCollaboration?.(id); setCreating(false); }
  return <section aria-label="四角色协作工作区"><Notice>{capabilities.error || list.error}</Notice>
    {!capabilities.loading && !supported ? <Empty title="后端尚未提供四角色协作">更新本机后端并重新检查连接后，再打开此工作区。</Empty> : supported && <>
      <div className="section-heading"><h2>项目协作组</h2><div className="button-group"><button disabled={list.loading} onClick={list.refresh}>刷新协作</button><button className="primary" onClick={() => setCreating(value => !value)} aria-expanded={creating}>{creating ? '收起准备' : '准备新协作'}</button></div></div>
      <p className="muted">四个角色各有独立运行、预算与权限。准备完成后，需另行启动。</p>
      <div hidden={!creating}><Prepare template={object(capabilities.data?.collaboration_template)} onAccepted={id => { open(id); list.refresh(); }} /></div>
      {list.loading && !list.data ? <p role="status">正在读取协作组…</p> : !rows(list.data?.items).length ? <Empty title="暂无协作组">准备固定离线研究，或按已有协作 ID 打开。</Empty> : <div className="table-scroll"><table aria-label="协作组列表"><thead><tr><th>协作 ID</th><th>模式</th><th>状态</th><th>来源组</th><th>操作</th></tr></thead><tbody>{rows(list.data?.items).map(item => <tr key={str(item.collaboration_id)}><td><code title={str(item.collaboration_id)}>{short(item.collaboration_id)}</code></td><td>{str(item.mode)}</td><td><Status value={item.status} /></td><td><code title={str(item.source_collaboration_id)}>{short(item.source_collaboration_id)}</code></td><td><button onClick={() => open(str(item.collaboration_id))}>打开协作</button></td></tr>)}</tbody></table></div>}
      <div className="pagination"><span className="small muted">每页 20 项 · 按协作 ID 升序</span><button disabled={!after} onClick={() => setAfter('')}>回到首批</button><button disabled={!list.data?.next_after_id} onClick={() => setAfter(str(list.data?.next_after_id))}>后续协作</button></div>
      <form className="inline-form panel" onSubmit={event => { event.preventDefault(); open(lookup.trim()); }}><label className="grow">协作 ID<input value={lookup} onChange={event => setLookup(event.target.value)} placeholder="粘贴协作 UUID" spellCheck={false} /></label><button disabled={!lookup.trim()}>按 ID 打开协作</button></form>
      {groupId && <Group key={groupId} groupId={groupId} active={props.active} open={open} openTimeline={props.openTimeline} changed={list.refresh} />}
    </>}
  </section>;
}

function Prepare({ template, onAccepted }: { template: JsonObject; onAccepted: (id: string) => void }) {
  const [goal, setGoal] = useState(str(object(object(template.program).goal).text));
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [frozen, setFrozen] = useState(false), action = useAction();
  const attempt = useRef<Inputs['prepareCollaboration'] | null>(null);
  const program = object(template.program);
  return <section className="panel" aria-labelledby="prepare-collaboration"><h2 id="prepare-collaboration">准备四角色研究</h2><p>使用 Aster、Birch、Cedar 合成资料与 Mock 模型。协调、取证、核验、撰写依次推进。</p>
    <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
      if (!attempt.current) {
        if (!goal.trim() || !Object.keys(program).length) throw new Error('请填写目标，并重新读取后端固定模板');
        attempt.current = { key: crypto.randomUUID(), request: { schema_version: 1, program: { ...program, goal: { ...object(program.goal), text: goal.trim() } },
          private_inputs: Object.entries(notes).filter(([, value]) => value.length > 0).map(([role, value]) => ({ schema_version: 1, role, notes: [value] })) } };
        setFrozen(true);
      }
      const value = await call(window.agentgit.prepareCollaboration(attempt.current));
      attempt.current = null; setFrozen(false); setNotes({});
      onAccepted(str(value.collaboration_id));
    }); }}><label>协作目标<textarea rows={3} maxLength={4000} value={goal} disabled={frozen} onChange={event => setGoal(event.target.value)} /></label>
      <details><summary>角色预算、权限与私有备注</summary><p className="muted small">采用后端固定预算。备注只进入对应角色的私有输入，不在协作公开查询中返回。</p>{rows(program.roles).map(role => <div className="collaboration-policy" key={str(role.role)}><h3>{ROLE_NAMES[str(role.role)]}</h3><p className="small">权限：{Array.isArray(role.permissions) ? role.permissions.join(' · ') : '未知'}</p><p className="small">最多 {count(object(role.limits).max_model_calls)} 次模型、{count(object(role.limits).max_tool_calls)} 次工具、{count(object(role.limits).max_steps)} 步；墙钟 {count(object(role.limits).max_wall_ms)} ms。</p><label>{ROLE_NAMES[str(role.role)]} 私有备注<textarea rows={2} maxLength={4000} value={notes[str(role.role)] ?? ''} disabled={frozen} onChange={event => setNotes(previous => ({ ...previous, [str(role.role)]: event.target.value }))} /></label></div>)}<details><summary>固定程序与完整预算原文</summary><JsonView value={program} /></details></details>
      <Notice>{action.error}</Notice><p className="small muted">{frozen ? '已冻结原请求及幂等键。结果未确认时，只能手动重试原准备操作；重开窗口不会自动提交。' : '本次只准备四个 Run，不启动执行。'}</p><button className="primary" disabled={action.busy || !goal.trim()}>{action.busy ? '正在准备…' : frozen ? '重试原准备请求' : '确认准备协作'}</button>
    </form></section>;
}

type Pending = { kind: 'start' | 'recorded'; input: Inputs['startCollaboration'] } | { kind: 'control'; input: Inputs['controlCollaboration'] };
function Group({ groupId, active, open, openTimeline, changed }: { groupId: string; active: boolean; open: (id: string) => void; openTimeline: (id: string) => void; changed: () => void }) {
  const group = useRead(() => active ? call(window.agentgit.getCollaboration({ groupId })) : Promise.resolve(null), [groupId, active], active ? 2000 : 0, groupId, active);
  const signature = rows(group.data?.roles).map(role => `${str(role.run_id)}:${str(role.status)}:${str(role.job_status)}:${str(role.control_status)}`).join('|');
  const members = useRead(() => active ? Promise.all(rows(group.data?.roles).map(role => call(window.agentgit.getRun({ runId: str(role.run_id) })))) : Promise.resolve([]), [groupId, active, signature], active ? 2000 : 0, groupId, active);
  const [pending, setPending] = useState<Pending | null>(null), [accepted, setAccepted] = useState('');
  const action = useAction(), pendingRef = useRef<Pending | null>(null);
  async function execute(request: Pending) {
    pendingRef.current = request; setPending(request);
    const result = request.kind === 'control' ? await call(window.agentgit.controlCollaboration(request.input))
      : request.kind === 'start' ? await call(window.agentgit.startCollaboration(request.input)) : await call(window.agentgit.recordCollaboration(request.input));
    pendingRef.current = null; setPending(null); setAccepted('请求已接受，请以当前组与各角色状态确认结果。'); group.refresh(); changed();
    if (request.kind === 'recorded') open(str(result.collaboration_id));
  }
  const submit = (kind: 'start' | 'recorded' | 'pause' | 'resume' | 'cancel') => void action.run(async () => {
    const request = pendingRef.current ?? (kind === 'start' || kind === 'recorded' ? { kind, input: { groupId, key: crypto.randomUUID() } } as Pending
      : { kind: 'control', input: { groupId, action: kind, expectedRevision: Number(group.data?.revision), key: crypto.randomUUID() } } as Pending);
    await execute(request);
  });
  const state = str(group.data?.status), locked = action.busy || pending !== null || !group.data || !active;
  const memberNames = new Map(rows(group.data?.roles).map(role => [str(role.run_id), ROLE_NAMES[str(role.role)] ?? str(role.role)]));
  return <section className="collaboration-detail" aria-label="当前协作"><div className="section-heading"><h2>当前协作</h2><button disabled={group.loading} onClick={group.refresh}>查询当前状态</button></div><p><code>{groupId}</code></p><Notice>{group.error || members.error || action.error}</Notice>
    {group.loading && !group.data && <p role="status">正在读取协作…</p>}
    {group.data && <><div className="run-summary"><Status value={group.data.status} /><span>模式 <strong>{str(group.data.mode)}</strong></span><span>控制修订 {count(group.data.revision)}</span><span>{group.data.quiescent ? '所有角色已静止' : '仍有排队或在途角色'}</span></div>
      <p className="small muted">组控制收据表示请求已接受。暂停或取消落实前，在途调用仍可能结算；结果未知时需核对，不会自动重发。</p>
      <div className="button-group"><button className="primary" disabled={locked || state !== 'prepared'} onClick={() => submit('start')}>启动协作</button><button disabled={locked || state !== 'running'} onClick={() => submit('pause')}>请求整组暂停</button><button disabled={locked || state !== 'paused' || group.data.quiescent !== true || group.data.has_unknown === true || group.data.has_failed === true} onClick={() => submit('resume')}>恢复整组</button><button disabled={locked || group.data.all_terminal === true || !['prepared', 'running', 'paused'].includes(state)} onClick={() => submit('cancel')}>请求整组取消</button><button disabled={locked || state !== 'completed' || group.data.mode !== 'live'} onClick={() => submit('recorded')}>准备 Recorded 协作</button></div>
      {pending && <div className="meter-attention"><p>原请求结果未确认。保留动作、控制修订和幂等键；查询不会重发。</p><p className="small">{pending.kind === 'control' ? `${pending.input.action} · 修订 ${pending.input.expectedRevision}` : pending.kind} · <code>{pending.input.key}</code></p><button disabled={action.busy || !active} onClick={() => void action.run(() => execute(pending))}>重试原组请求</button></div>}
      {accepted && <p role="status">{accepted}</p>}
      {group.data.has_unknown === true && <Notice>存在结果未知的角色。请打开其时间线核对来源，组恢复保持禁用。</Notice>}
      {group.data.source_collaboration_id && <p>来源协作 <button className="text-button" onClick={() => open(str(group.data?.source_collaboration_id))}>{str(group.data.source_collaboration_id)}</button></p>}
      <h3>四个独立角色</h3><div className="table-scroll"><table aria-label="协作角色"><thead><tr><th>角色 / Run</th><th>状态 / 阶段</th><th>逻辑模型 / 工具 / 步数</th><th>物理意图 / 复用 / 未知</th><th>操作</th></tr></thead><tbody>{rows(group.data.roles).map(role => {
        const run = members.data?.find(item => item.id === role.run_id), adapter = object(run?.adapter), usage = object(adapter.logical_usage);
        return <tr key={str(role.run_id)}><td><strong className="table-goal">{ROLE_NAMES[str(role.role)]}</strong><code title={str(role.run_id)}>{short(role.run_id)}</code></td><td><Status value={role.status} /><p className="small muted">{str(adapter.phase) || '待读取'}</p></td><td>{count(usage.model_calls)} / {count(usage.tool_calls)} / {count(usage.steps)}</td><td>{count(adapter.physical_call_intents)} / {count(adapter.reused_calls)} / {count(adapter.unknown_calls)}</td><td><button onClick={() => openTimeline(str(role.run_id))}>查看角色时间线</button><details><summary>预算与权限</summary><JsonView value={{ limits: adapter.limits ?? null, permissions: adapter.permissions ?? null }} /></details></td></tr>;
      })}</tbody></table></div><p className="small muted">物理意图表示调用尝试的登记，不能直接当作已执行次数或账单。Mock 用量按 UTF-8 字节记；Recorded 保留历史逻辑用量，本次调用另列。</p>
      <Messages groupId={groupId} active={active} signature={signature} names={memberNames} openTimeline={openTimeline} />
      <Shared groupId={groupId} active={active} signature={signature} names={memberNames} openTimeline={openTimeline} />
    </>}
  </section>;
}

function Messages({ groupId, active, signature, names, openTimeline }: { groupId: string; active: boolean; signature: string; names: Map<string, string>; openTimeline: (id: string) => void }) {
  const [after, setAfter] = useState('');
  const page = useRead(() => active ? call(window.agentgit.getCollaborationMessages({ groupId, ...(after ? { afterId: after } : {}), limit: 30 })) : Promise.resolve(null), [groupId, active, signature, after], active ? 2000 : 0, `${groupId}:${after}`, active);
  return <section className="collaboration-evidence"><h3>消息与因果边</h3><Notice>{page.error}</Notice><p className="small muted">按消息 ID 升序列出；父事件与发送、接收地址分别保留，不按列表顺序推断执行先后。</p>{rows(page.data?.items).map(item => <details key={str(item.message_id)}><summary>{str(item.kind)} · {names.get(str(item.sender))} → {names.get(str(item.receiver))} · {item.received_seq === null ? '未接收' : '已接收'}</summary><div className="button-group"><button onClick={() => openTimeline(str(item.sender))}>打开发送角色</button><button onClick={() => openTimeline(str(item.receiver))}>打开接收角色</button></div><p className="small">父事件序号 {count(object(item.parent_event_id).seq)} · 发送序号 {count(item.sent_seq)} · 接收序号 {count(item.received_seq)}</p><JsonView value={item} /></details>)}{!page.loading && !rows(page.data?.items).length && <p className="muted">当前页暂无消息。</p>}<div className="pagination"><button disabled={!after} onClick={() => setAfter('')}>首批消息</button><button disabled={!page.data?.next_after_id} onClick={() => setAfter(str(page.data?.next_after_id))}>后续消息</button></div></section>;
}

function Shared({ groupId, active, signature, names, openTimeline }: { groupId: string; active: boolean; signature: string; names: Map<string, string>; openTimeline: (id: string) => void }) {
  const [cursor, setCursor] = useState<{ afterPath: string; afterVersion: number } | null>(null);
  const page = useRead(() => active ? call(window.agentgit.getCollaborationShared({ groupId, ...(cursor ?? {}), limit: 30 })) : Promise.resolve(null), [groupId, active, signature, cursor?.afterPath, cursor?.afterVersion], active ? 2000 : 0, `${groupId}:${cursor?.afterPath}:${cursor?.afterVersion}`, active);
  return <section className="collaboration-evidence"><h3>共享产物版本</h3><Notice>{page.error}</Notice><div className="table-scroll"><table aria-label="共享产物版本"><thead><tr><th>路径 / 版本</th><th>对象摘要</th><th>发布者</th></tr></thead><tbody>{rows(page.data?.items).map(item => <tr key={`${item.path}:${item.version}`}><td><code>{str(item.path)}</code> · v{count(item.version)}</td><td><code title={str(item.object_ref)}>{short(item.object_ref)}</code></td><td><button className="text-button" onClick={() => openTimeline(str(object(item.publisher_event).agent_run_id))}>{names.get(str(object(item.publisher_event).agent_run_id)) || short(object(item.publisher_event).agent_run_id)}</button><details><summary>发布地址与版本原文</summary><JsonView value={item} /></details></td></tr>)}</tbody></table></div>{!page.loading && !rows(page.data?.items).length && <p className="muted">当前页暂无发布。产物文件可在发布角色的时间线中读取。</p>}<div className="pagination"><button disabled={!cursor} onClick={() => setCursor(null)}>首批版本</button><button disabled={!page.data?.next_after} onClick={() => setCursor({ afterPath: str(object(page.data?.next_after).path), afterVersion: Number(object(page.data?.next_after).version) })}>后续版本</button></div></section>;
}
