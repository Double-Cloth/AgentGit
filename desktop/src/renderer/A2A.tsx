import React, { useRef, useState } from 'react';
import type { Inputs, JsonObject } from '../shared/bridge';
import { a2aProgram, a2aServer } from '../shared/a2a';
import { integer } from '../shared/model-config';
import { call, Empty, JsonView, Notice, object, rows, short, Status, str, useAction, useRead } from './ui';

export default function A2A({ active, openTimeline }: { active: boolean; openTimeline: (id: string) => void }) {
  const [after, setAfter] = useState(''), [creating, setCreating] = useState(false), [lookup, setLookup] = useState('');
  const capabilities = useRead(() => call(window.agentgit.capabilities()), []);
  const supported = Array.isArray(capabilities.data?.features) && capabilities.data.features.includes('a2a');
  const list = useRead(() => call(window.agentgit.listA2A({ ...(after ? { afterId: after } : {}), limit: 20 })), [after], active && supported ? 3000 : 0, after, active && supported);
  const find = useAction();
  return <section aria-label="A2A任务工作区"><Notice>{capabilities.error || list.error}</Notice>
    {!capabilities.loading && !supported ? <Empty title="后端尚未提供A2A">更新本机后端并重新检查连接。</Empty> : supported && <>
      <div className="section-heading"><h2>本人 A2A 任务</h2><div className="button-group"><button disabled={list.loading} onClick={list.refresh}>刷新任务</button><button className="primary" onClick={() => setCreating(value => !value)} aria-expanded={creating}>{creating ? '收起新建任务' : '新建 A2A 任务'}</button></div></div>
      <p className="muted">把公开资料整理交给明确登记的本机服务。发送与远端取消分别审批；Worker 凭据由后端预置。</p>
      <div hidden={!creating}><Prepare active={active} openTimeline={openTimeline} accepted={list.refresh} /></div>
      {list.loading && !list.data ? <p role="status">正在读取任务…</p> : !rows(list.data?.items).length ? <Empty title="暂无 A2A 任务">先发现服务卡片，再提交固定文本任务。已有提交可按原键只读确认。</Empty> : <div className="table-scroll"><table aria-label="本人A2A任务"><thead><tr><th>Run / Job</th><th>模式</th><th>本地状态</th><th>操作</th></tr></thead><tbody>{rows(list.data?.items).map(item => <tr key={str(item.id)}><td><code title={str(item.id)}>{short(item.id)}</code><p className="small muted">Job {short(item.job_id)}</p></td><td>{str(item.mode)}</td><td><Status value={item.status} /></td><td><button onClick={() => openTimeline(str(item.id))}>查看任务时间线</button></td></tr>)}</tbody></table></div>}
      <div className="pagination"><span className="small muted">每页20项 · 按创建时间倒序</span><button disabled={!after} onClick={() => setAfter('')}>最新任务</button><button disabled={!list.data?.next_after_id} onClick={() => setAfter(str(list.data?.next_after_id))}>更早任务</button></div>
      <form className="inline-form panel" onSubmit={event => { event.preventDefault(); void find.run(async () => { const receipt = await call(window.agentgit.getA2ASubmission({ key: lookup.trim() })); openTimeline(str(receipt.run_id)); }); }}><label className="grow">提交原键<input value={lookup} maxLength={200} spellCheck={false} onChange={event => setLookup(event.target.value)} placeholder="粘贴 A2A 提交原键" /></label><button disabled={find.busy || !lookup.trim()}>按原键只读确认</button><Notice>{find.error}</Notice></form>
    </>}
  </section>;
}

function Prepare({ active, openTimeline, accepted }: { active: boolean; openTimeline: (id: string) => void; accepted: () => void }) {
  const [endpoint, setEndpoint] = useState('http://127.0.0.1:8123'), [identity, setIdentity] = useState('public-research'), [principal, setPrincipal] = useState('operator');
  const [binding, setBinding] = useState<JsonObject | null>(null), [texts, setTexts] = useState(['整理公开资料：Aster、Birch、Cedar。']), [branch, setBranch] = useState('main');
  const [pending, setPending] = useState<Inputs['submitA2A'] | null>(null), [receipt, setReceipt] = useState<JsonObject | null>(null);
  const [rejection, setRejection] = useState('');
  const attempt = useRef<Inputs['submitA2A'] | null>(null), discover = useAction(), action = useAction();
  const branches = useRead(() => call(window.agentgit.listBranches({})), [], 0, 'branches', active);
  const locked = Boolean(pending || receipt) || action.busy;
  const selected = rows(branches.data?.items).find(item => item.name === branch);
  const pinned = pending?.request;
  const version = integer(pinned ? pinned.expected_version : selected?.version);
  const head = str(pinned ? pinned.expected_head : selected?.head_commit_id);
  function editServer(change: () => void) { change(); setBinding(null); discover.clearError(); }
  async function submit(saved: Inputs['submitA2A'], confirm = false) {
    const reply = await (confirm ? window.agentgit.getA2ASubmission({ key: saved.key }) : window.agentgit.submitA2A(saved));
    if (!confirm && !reply.ok && reply.error.code === 'version_conflict') {
      attempt.current = null; setPending(null);
      setRejection(`此次提交已明确拒绝，未受理。请刷新分支前置条件，再明确提交；已填消息与服务卡片保持。原键：${saved.key}`);
    }
    const result = await call(Promise.resolve(reply));
    setRejection('');
    setReceipt(result); accepted();
  }
  return <section className="panel" aria-labelledby="a2a-prepare-title"><h2 id="a2a-prepare-title">准备远端任务</h2>
    <div className="inline-form"><label className="grow">登记地址<input value={endpoint} disabled={locked || discover.busy} maxLength={100} onChange={event => editServer(() => setEndpoint(event.target.value))} spellCheck={false} /></label><label>服务名称<input value={identity} disabled={locked || discover.busy} maxLength={64} onChange={event => editServer(() => setIdentity(event.target.value))} /></label><label>非秘密主体<input value={principal} disabled={locked || discover.busy} maxLength={64} onChange={event => editServer(() => setPrincipal(event.target.value))} /></label></div>
    <button disabled={locked || discover.busy || !active} onClick={() => void discover.run(async () => { setBinding(null); setBinding(await call(window.agentgit.bindA2A({ server: a2aServer(endpoint.trim(), identity.trim(), principal.trim()) }))); })}>{discover.busy ? '正在发现…' : '发现并核对服务卡片'}</button><Notice>{discover.error}</Notice>
    {binding && <><p role="status">服务卡片已发现。协议 v1.0.1 · HTTP+JSON 1.0 · 仅固定公开资料文本。</p><details><summary>已固定的服务与卡片</summary><JsonView value={binding} /></details></>}
    <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
      if (!attempt.current) {
        setRejection('');
        if (!binding) throw new Error('请先发现服务卡片');
        if (!selected || version === undefined || branches.loading) throw new Error('请刷新并选择有效分支版本');
        attempt.current = { key: crypto.randomUUID(), request: { schema_version: 1, program: a2aProgram(binding, texts, () => crypto.randomUUID()), branch_name: branch, expected_head: selected.head_commit_id ?? null, expected_version: version } };
        setPending(attempt.current);
      }
      await submit(attempt.current);
    }); }}>
      {texts.map((value, index) => <label key={index}>{index === 0 ? '首条公开消息' : `预声明补充消息 ${index}`}<textarea rows={3} maxLength={4000} disabled={locked} value={value} onChange={event => setTexts(previous => previous.map((text, i) => i === index ? event.target.value : text))} /></label>)}
      <div className="button-group"><button type="button" disabled={locked || texts.length >= 4} onClick={() => setTexts(previous => [...previous, ''])}>添加预声明补充消息</button><button type="button" disabled={locked || texts.length <= 1} onClick={() => setTexts(previous => previous.slice(0, -1))}>移除末条消息</button></div>
      <p className="small muted">补充消息只在服务要求输入时依次使用，每条发送另行审批。最多16次轮询、间隔1秒、40步／工具、60秒原始期限，单响应64 KiB、产物合计256 KiB／9文件；无模型调用。</p>
      <label>目标分支<select value={branch} disabled={locked} onChange={event => setBranch(event.target.value)}>{rows(branches.data?.items).map(item => <option key={str(item.name)}>{str(item.name)}</option>)}</select></label><button type="button" disabled={locked || branches.loading} onClick={branches.refresh}>刷新分支前置条件</button><Notice>{branches.error}</Notice>
      <p className="small muted" role="status">{pending ? '以下为本次固定的分支前置条件，重试保持原值。' : branches.loading ? '正在读取分支前置条件…' : '以下为最近读取的分支前置条件，提交时由后端再次核验。'}</p>
      <dl className="facts" aria-label="A2A分支前置条件"><div><dt>分支</dt><dd>{str(pinned?.branch_name) || branch}</dd></div><div><dt>版本</dt><dd>{version === undefined ? '待读取' : version}</dd></div><div><dt>HEAD</dt><dd><code>{head || (version === undefined ? '待读取' : '无 HEAD')}</code></dd></div></dl>
      {rejection && <p role="status">{rejection}</p>}
      {!pending && <button className="primary" disabled={action.busy || !binding || !branches.data || branches.loading || texts.some(text => !text.trim()) || !active}>确认提交 A2A 任务</button>}
      {pending && !receipt && <div className="meter-attention"><p>原请求已固定，结果尚未确认。保留此键，重开后可按原键查询；查询不会发送协议消息。</p><p className="small">原键 <code>{pending.key}</code></p><div className="button-group"><button type="button" disabled={action.busy || !active} onClick={() => void action.run(() => submit(pending, true))}>只读确认原提交</button><button type="button" disabled={action.busy || !active} onClick={() => void action.run(() => submit(pending))}>显式重试原提交</button></div></div>}
      <Notice>{action.error}</Notice>{receipt && <div role="status"><p>已受理，只表示本地任务入队。发送前仍需在时间线核对精确审批。</p><p>原键 <code>{pending?.key}</code></p><button type="button" onClick={() => openTimeline(str(receipt.run_id))}>打开新 A2A 时间线</button><JsonView value={receipt} /></div>}
    </form>
  </section>;
}
