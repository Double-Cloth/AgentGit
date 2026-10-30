import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import type { BinaryChunk, ExecutionResource, JsonObject } from '../shared/bridge';
import { matchesEvidence } from '../shared/diagnosis';
import BudgetSummary from './BudgetSummary';
import RunAdapterSummary from './RunAdapterSummary';
import { fixedProgramKind } from '../shared/fixed-program';
import { call, date, Empty, err, JsonView, Notice, object, rows, short, Status, str, useAction, useRead, type EvidenceTarget, type PageProps } from './ui';
const HistoryGraph = lazy(() => import('./HistoryGraph'));

export default function Timeline(props: PageProps) {
  const [lookup, setLookup] = useState(props.runId), [branch, setBranch] = useState('');
  const [afterCommit, setAfterCommit] = useState<string | undefined>(), [tab, setTab] = useState<'state' | 'execution' | 'files'>('execution');
  const [callId, setCallId] = useState('');
  const run = useRead(() => props.runId ? call(window.agentgit.getRun({ runId: props.runId })) : Promise.resolve({} as JsonObject), [props.runId], props.runId ? 3000 : 0);
  const branches = useRead(() => call(window.agentgit.listBranches({})), []);
  const branchName = branch || str(run.data?.branch_name) || 'main';
  const history = useRead(() => call(window.agentgit.listCommits({ branchName, ...(afterCommit ? { afterCommit } : {}), limit: 50 })), [branchName, afterCommit, run.data?.checkpoint_commit_id]);
  const commitId = props.selectedCommit || str(run.data?.checkpoint_commit_id);
  const commit = useRead(() => commitId ? call(window.agentgit.getCommit({ commitId })) : Promise.resolve({} as JsonObject), [commitId]);
  const calls = useRead(() => props.runId ? call(window.agentgit.listExecutions({ runId: props.runId, limit: 100 })) : Promise.resolve({} as JsonObject), [props.runId, run.data?.status, run.data?.checkpoint_commit_id], 0, props.runId);
  const events = useEvents(props.runId);
  const control = useAction(), keys = useRef(new Map<string, string>());
  useEffect(() => { props.setSelectedCommit(''); setCallId(''); setAfterCommit(undefined); setBranch(''); }, [props.runId]);
  if (!props.runId) return <><div className="page-heading"><div><span className="eyebrow">TIMELINE</span><h1>时间线</h1><p>选择一次运行，追溯检查点与执行证据。</p></div></div><form className="inline-form panel" onSubmit={event => { event.preventDefault(); props.setRunId(lookup.trim()); }}><label className="grow">Run ID<input value={lookup} onChange={event => setLookup(event.target.value)} placeholder="粘贴 Run UUID" /></label><button disabled={!lookup.trim()}>打开运行</button></form></>;
  const snapshot = object(commit.data?.snapshot), fixedProgram = Boolean(fixedProgramKind(run.data));
  const selectedCall = callId || str(rows(calls.data?.items)[0]?.id);
  const change = (id: string) => { props.setSelectedCommit(id); setTab('state'); };
  const doControl = (action: 'pause' | 'resume' | 'cancel') => void control.run(async () => {
    const version = Number(run.data?.state_version), identity = `${props.runId}:${action}:${version}`;
    if (!keys.current.has(identity)) keys.current.set(identity, crypto.randomUUID());
    await call(window.agentgit.controlRun({ runId: props.runId, action, expectedVersion: version, key: keys.current.get(identity)! })); run.refresh();
  });
  return <><div className="page-heading"><div><span className="eyebrow">TIMELINE</span><h1>时间线</h1><p><code>{props.runId}</code> <Status value={run.data?.status} /></p></div><button disabled={!commitId} onClick={() => props.openBranch(props.runId, commitId)}>{fixedProgram ? '分支方式说明 →' : '从所选检查点分支 →'}</button></div>
    <Notice>{run.error || history.error || calls.error || control.error}</Notice>
    {!fixedProgram && <div className="button-group"><button onClick={() => props.openDiagnosis(props.runId)}>查看故障报告</button><button onClick={() => props.openRepair(props.runId, props.selectedCommit)}>在修复实验室核对</button></div>}
    {props.eventFocus?.runId === props.runId && <FocusedEvent key={`${props.eventFocus.runId}:${props.eventFocus.seq}:${props.eventFocus.eventId}`} target={props.eventFocus} />}
    {run.data?.status === 'queued' && <p className="muted" role="status">任务尚在排队，未取得执行进展。API 可用不代表 Worker 已就绪；若持续等待，请在后端检查 Worker 的启动状态。</p>}
    {run.data && <><div className="run-summary"><span>模式 <strong>{str(run.data.mode) || 'standard'}</strong></span><span>分支 <code>{str(run.data.branch_name)}</code></span><span>控制请求 <code>{str(run.data.control_request) || '无'}</code> / <code>{str(run.data.control_status) || '无'}</code></span><div className="button-group"><button disabled={control.busy || !['queued', 'running', 'waiting_approval'].includes(str(run.data.status))} onClick={() => doControl('pause')}>请求暂停</button><button disabled={control.busy || run.data.status !== 'paused'} onClick={() => doControl('resume')}>恢复</button><button disabled={control.busy || !['queued', 'running', 'paused', 'waiting_approval', 'outcome_unknown'].includes(str(run.data.status))} onClick={() => doControl('cancel')}>请求取消</button></div></div>
    {run.data.mode === 'recorded' && !fixedProgram && <div className="explanation"><strong>Recorded · 历史验证与任务成功分别判断</strong><p>本 Run 不启动执行容器。逻辑状态、历史用量、实际零调用与逐检查点证明见下方 Replay 报告。查看原执行证据须明确打开来源 Run。</p><JsonView value={run.data.replay} />{object(run.data.replay).source_run_id && <button onClick={() => props.openTimeline(str(object(run.data?.replay).source_run_id))}>打开来源运行</button>}</div>}
    <RunAdapterSummary run={run.data} openSource={props.openTimeline} /><BudgetSummary run={run.data} />{!fixedProgram && <details className="budget-details"><summary>预算与来源原文</summary><JsonView value={{ budget: run.data.budget ?? null, replay: run.data.replay ?? null, origin_run_id: run.data.origin_run_id ?? null }} /></details>}</>}
    <div className="timeline-layout"><section className="timeline-left"><div className="section-heading"><h2>检查点</h2><label className="compact-label">分支<select value={branchName} onChange={event => { setBranch(event.target.value); setAfterCommit(undefined); }}>{rows(branches.data?.items).map(item => <option key={str(item.name)}>{str(item.name)}</option>)}</select></label></div>
      {history.loading ? <p role="status">读取历史…</p> : !rows(history.data?.items).length ? <Empty title="尚无检查点">任务取得进展后会显示持久历史。</Empty> : <><Suspense fallback={<p>加载关系图…</p>}><HistoryGraph commits={rows(history.data?.items)} selected={commitId} onSelect={change} /></Suspense><div className="checkpoint-list" aria-label="检查点列表">{rows(history.data?.items).map((item, index) => <button className={item.commit_id === commitId ? 'selected' : ''} key={str(item.commit_id)} onClick={() => change(str(item.commit_id))}><span>{index === 0 && !afterCommit ? 'HEAD' : 'Commit'}</span><code>{short(item.commit_id)}</code><span className="small muted">{date(item.created_at)}</span></button>)}</div><div className="pagination"><button disabled={!afterCommit} onClick={() => setAfterCommit(undefined)}>最新</button><button disabled={!history.data?.has_more} onClick={() => setAfterCommit(str(history.data?.next_after_commit))}>更早检查点</button></div></>}
      <div className="section-heading"><h2>事件</h2><span className="muted small">游标 {events.cursor}</span></div><Notice>{events.error}</Notice><div className="events">{events.items.map(item => <details key={String(item.seq)}><summary><code>{String(item.seq).padStart(3, '0')}</code> {str(item.type)} <span className="muted small">{str(item.logical_step_id)}</span></summary><JsonView value={item} /></details>)}{!events.items.length && <p className="muted">暂无事件</p>}</div>
    </section><section className="inspector" aria-labelledby="inspector-title"><div className="section-heading"><h2 id="inspector-title">证据详情</h2><code title={commitId}>{short(commitId)}</code></div><div className="tabs" role="tablist" aria-label="详情类别">{([['state', '逻辑状态'], ['execution', '执行证据'], ['files', '产物文件']] as const).map(([id, label]) => <button role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} key={id} onClick={() => setTab(id)}>{label}</button>)}</div>
      {tab === 'state' ? <><Notice>{commit.error}</Notice>{commit.loading ? <p role="status">读取所选检查点…</p> : <><h3>所选版本状态</h3><JsonView value={snapshot.data} /><details><summary>Commit / 环境原文</summary><JsonView value={{ manifest: commit.data?.manifest ?? null, environment: commit.data?.environment ?? null }} /></details></>}</> : tab === 'files' ? <Artifacts key={commitId} commitId={commitId} /> : <><p className="muted small">以下是本 Run 的实际执行账本，与左侧所选历史状态分别展示；恢复复用明确保留物理来源。</p>{!rows(calls.data?.items).length ? <Empty title={fixedProgram ? "本 Run 没有终端 / Python 调用" : "本 Run 暂无执行调用"}>{fixedProgram ? "固定工具及逻辑用量见上方适配器摘要，保存的状态与结果见逻辑状态和产物文件。" : "这里仅列终端与 Python 执行调用；模型计量见上方。未发布的执行证据不会补成成功结果。"}</Empty> : <><label>执行调用<select value={selectedCall} onChange={event => setCallId(event.target.value)}>{rows(calls.data?.items).map(item => <option value={str(item.id)} key={str(item.id)}>{str(item.provider)} · {str(item.logical_step_id)} · attempt {String(item.attempt)}</option>)}</select></label><Execution key={props.runId + selectedCall} runId={props.runId} callId={selectedCall} /><p className="small muted">每次最多显示 100 个执行调用。</p></>}</>}
      <Approvals runId={props.runId} version={run.data?.state_version} />
    </section></div></>;
}

function FocusedEvent({ target }: { target: EvidenceTarget }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const event = useRead(async () => {
    const result = await call(window.agentgit.getEvents({ runId: target.runId, afterSeq: target.seq - 1, limit: 1 }));
    const row = rows(result.items)[0];
    if (!row || !matchesEvidence(row, target)) throw new Error('报告引用的精确事件不存在或身份不符；请核对项目及证据完整性');
    return row;
  }, [target.runId, target.seq, target.eventId]);
  useEffect(() => { if (!event.loading) heading.current?.focus(); }, [event.loading]);
  return <section className="focused-event" aria-labelledby="focused-event-title"><h2 ref={heading} tabIndex={-1} id="focused-event-title">定位证据 · 事件 {target.seq}</h2><p className="small muted">Run <code>{target.runId}</code> · ID {target.eventId}。独立按精确引用读取，不受最近 2000 条事件窗口限制。</p><Notice>{event.error}</Notice>{event.loading ? <p role="status">读取目标事件…</p> : event.data && <JsonView value={event.data} />}</section>;
}

function useEvents(runId: string) {
  const [items, setItems] = useState<JsonObject[]>([]), [cursor, setCursor] = useState(0), [error, setError] = useState('');
  useEffect(() => {
    let active = true, after = 0, timer: ReturnType<typeof setTimeout>;
    setItems([]); setCursor(0); setError('');
    async function poll() {
      try {
        const value = await call(window.agentgit.getEvents({ runId, afterSeq: after, limit: 100 }));
        if (!active) return;
        const batch = rows(value.items); after = Number(value.next_after_seq);
        setCursor(after); setItems(previous => [...previous, ...batch].slice(-2000)); setError('');
        timer = setTimeout(() => void poll(), batch.length === 100 ? 0 : 2000);
      } catch (failure) { if (active) { setError(err(failure)); timer = setTimeout(() => void poll(), 4000); } }
    }
    if (runId) void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [runId]);
  return { items, cursor, error };
}

function Execution({ runId, callId }: { runId: string; callId: string }) {
  const detail = useRead(() => call(window.agentgit.getExecution({ runId, callId })), [runId, callId], 4000);
  const [resource, setResource] = useState<ExecutionResource>('stdout'), [offset, setOffset] = useState(0);
  const resources = Array.isArray(detail.data?.resources) ? detail.data.resources.map(str) : [];
  const selected = resources.includes(resource) ? resource : resources[0] as ExecutionResource | undefined;
  const content = useRead(() => selected ? call(window.agentgit.getExecutionContent({ runId, callId, resource: selected, offset, limit: 65536 })) : Promise.resolve(null), [runId, callId, selected, offset, detail.data?.execution_status]);
  const result = object(detail.data?.result);
  return <><Notice>{detail.error || content.error}</Notice>{result.error_code === 'sandbox_unavailable' && <Notice>执行环境不可用（sandbox_unavailable）。请在后端运行 agentgit doctor --sandbox，检查 Docker Linux 引擎与已审阅的固定镜像。此错误不说明具体缺少哪一项；桌面不会自动安装或更改环境。</Notice>}{detail.loading ? <p role="status">读取执行证据…</p> : <><dl className="facts execution-facts"><div><dt>执行状态</dt><dd>{str(detail.data?.execution_status)}</dd></div><div><dt>退出码</dt><dd>{result.exit_code === null || result.exit_code === undefined ? '未知 / 未发布' : String(result.exit_code)}</dd></div><div><dt>耗时</dt><dd>{detail.data?.wall_ms === null ? '未知' : `${String(detail.data?.wall_ms)} ms`}</dd></div><div><dt>清理确认</dt><dd>{detail.data?.cleanup_confirmed ? '已确认' : '未确认'}</dd></div><div><dt>截断</dt><dd>{result.stdout_truncated || result.stderr_truncated ? '有截断，见结果原文' : result.schema_version ? '未截断' : '未知'}</dd></div></dl>
    <p className="small muted">逻辑 Run <code>{short(detail.data?.run_id)}</code> · 物理 Run <code>{short(detail.data?.physical_run_id)}</code> · 调用 <code>{short(detail.data?.physical_call_id)}</code></p>
    <details><summary>执行参数与完整结果</summary><JsonView value={{ arguments: detail.data?.arguments ?? null, result: detail.data?.result ?? null }} /></details>
    <label>原始证据<select value={selected ?? ''} onChange={event => { setResource(event.target.value as ExecutionResource); setOffset(0); }}>{resources.map(item => <option key={item}>{item}</option>)}</select></label>
    {content.loading ? <p role="status">读取证据分段…</p> : content.data && <Chunk chunk={content.data} />}
    <div className="pagination"><button disabled={!offset} onClick={() => setOffset(0)}>回到开头</button><button disabled={content.data?.nextOffset === null || !content.data} onClick={() => setOffset(content.data!.nextOffset!)}>下一段原字节</button></div></>}</>;
}
function Chunk({ chunk }: { chunk: BinaryChunk }) {
  const bytes = Uint8Array.from(atob(chunk.base64), char => char.charCodeAt(0));
  let text: string, binary = false;
  try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); if (text.includes('\0')) throw new Error(); }
  catch { binary = true; text = Array.from(bytes.subarray(0, 16384), byte => byte.toString(16).padStart(2, '0')).join(' '); }
  return <><p className="small muted">{binary ? '二进制 · 十六进制预览（最多 16 KiB）' : 'UTF-8 文本 · 不执行其中的 HTML 或代码'}<br />原字节 {chunk.offset}–{chunk.offset + bytes.length} / {chunk.totalBytes} · <code>{short(chunk.objectRef)}</code></p><pre className="code" tabIndex={0}>{text || '（空内容）'}</pre></>;
}
function Artifacts({ commitId }: { commitId: string }) {
  const [after, setAfter] = useState<string | undefined>(), [selected, setSelected] = useState('');
  const list = useRead(() => commitId ? call(window.agentgit.getCommitArtifacts({ commitId, ...(after ? { afterId: after } : {}) })) : Promise.resolve({} as JsonObject), [commitId, after]);
  const action = useAction(), [message, setMessage] = useState('');
  const preview = useRead(() => selected ? call(window.agentgit.readArtifact({ artifactId: selected })) : Promise.resolve(null), [selected]);
  return <><h3>所选 Commit 的文件</h3><p className="small muted">同名文件保持所选版本，下载按产物 ID，不转到最新 Run。</p><Notice>{list.error || action.error || preview.error}</Notice>{!rows(list.data?.items).length ? <p className="muted">该检查点没有产物。</p> : <ul className="files">{rows(list.data?.items).map(item => <li key={str(item.id)}><button className="text-button file-name" onClick={() => setSelected(str(item.id))}>{str(item.relative_path)}</button><span className="small muted">{short(item.object_id)}</span><button disabled={action.busy} onClick={() => void action.run(async () => { const value = await call(window.agentgit.exportArtifact({ artifactId: str(item.id) })); setMessage(value.cancelled ? '已取消保存' : '所选版本已保存'); })}>另存为</button></li>)}</ul>}
    <p role="status" className="small muted">{message}</p><div className="pagination"><button disabled={!after} onClick={() => setAfter(undefined)}>首页</button><button disabled={!list.data?.next_after_id} onClick={() => setAfter(str(list.data?.next_after_id))}>更多文件</button></div>{preview.data && <Chunk chunk={preview.data} />}</>;
}
function Approvals({ runId, version }: { runId: string; version: import('../shared/bridge').Json | undefined }) {
  const approvals = useRead(() => call(window.agentgit.getApprovals({ runId })), [runId, version], 4000), action = useAction();
  const keys = useRef(new Map<string, string>());
  const pending = rows(approvals.data?.items).filter(item => item.status === 'pending');
  return <section className="approvals"><h3>审批</h3><Notice>{approvals.error || action.error}</Notice>{!pending.length ? <p className="muted small">暂无待处理审批；过期与版本冲突以提交时后端校验为准。</p> : pending.map(item => <details open key={str(item.id)}><summary>待审批动作 <code>{short(item.id)}</code></summary><JsonView value={item.request} /><p className="small muted">到期 {date(item.expires_at)} · 批准不会提升工具权限。</p><div className="button-group">{(['approve', 'reject'] as const).map(decision => <button key={decision} disabled={action.busy} onClick={() => void action.run(async () => {
    const identity = `${str(item.id)}:${String(item.version)}:${decision}`;
    if (!keys.current.has(identity)) keys.current.set(identity, crypto.randomUUID());
    await call(window.agentgit.decideApproval({ approvalId: str(item.id), decision, expectedVersion: Number(item.version), key: keys.current.get(identity)! })); approvals.refresh();
  })}>{decision === 'approve' ? '批准此动作' : '拒绝此动作'}</button>)}</div></details>)}</section>;
}
