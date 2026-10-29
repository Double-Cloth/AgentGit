import React, { useEffect, useRef, useState } from 'react';
import type { JsonObject, Reply } from '../shared/bridge';
import { hasFeature, knownRejection } from '../shared/experiments';
import { MERGE_STATUS, mergeReviewGate, mergeVersion } from '../shared/merge';
import { call, date, JsonView, Notice, object, pretty, rows, short, str, useAction, useRead, type PageProps } from './ui';

type Attempt = { label: string; send: () => Promise<Reply<JsonObject>>; accepted?: (value: JsonObject) => void };
type Begin = (attempt: Attempt) => void;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function identifier(value: string) { if (!UUID.test(value)) throw new Error('请输入完整 UUID'); return value; }
function parseObject(value: string, label: string): JsonObject {
  let data: unknown; try { data = JSON.parse(value); } catch { throw new Error(`${label}必须是合法 JSON`); }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error(`${label}必须是 JSON 对象`);
  return data as JsonObject;
}
function Detail({ title, value, open = false }: { title: string; value: JsonObject | JsonObject[] | string | null | undefined; open?: boolean }) {
  return <details open={open}><summary>{title}</summary><JsonView value={value} /></details>;
}

export default function MergeReview(props: PageProps & { active: boolean }) {
  const capabilities = useRead(() => call(window.agentgit.capabilities()), [], 5000, 'merge-capabilities');
  const supported = hasFeature(capabilities.data, 'semantic_merge');
  const [selected, setSelected] = useState(''), [lookup, setLookup] = useState(''), [after, setAfter] = useState<string | undefined>();
  const [attempt, setAttempt] = useState<Attempt | null>(null), pending = useRef<Attempt | null>(null), mounted = useRef(true);
  const action = useAction();
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const listing = useRead(() => supported ? call(window.agentgit.listMergeProposals({ ...(after ? { afterId: after } : {}) })) : Promise.resolve({ items: [] } as JsonObject), [supported, after], 0, 'merge-list');
  const report = useRead(() => selected ? call(window.agentgit.getMergeProposal({ proposalId: selected })) : Promise.resolve({} as JsonObject), [selected, supported], selected ? 5000 : 0, selected);
  const blocked = action.busy || attempt !== null || !props.active || !supported;
  function open(value: string) { const id = identifier(value); if (id === selected) report.refresh(); else setSelected(id); setLookup(id); }
  async function execute(next: Attempt) {
    const reply = await next.send();
    if (!mounted.current) return;
    if (!reply.ok) {
      if (knownRejection(reply.error.code) || ['unsupported', 'output_limit', 'connection_required', 'database_unavailable', 'repository_required', 'identity_missing', 'identity_invalid', 'busy'].includes(reply.error.code)) { pending.current = null; setAttempt(null); }
      report.refresh(); throw new Error(`${reply.error.message}（${reply.error.code}）`);
    }
    pending.current = null; setAttempt(null); next.accepted?.(reply.data); report.refresh(); listing.refresh();
  }
  const begin: Begin = next => { void action.run(async () => {
    if (pending.current) throw new Error('请先确认原请求结果');
    pending.current = next; setAttempt(next); await execute(next);
  }); };
  return <div className="merge-review"><div className="page-heading"><div><h1>合并审阅</h1><p>核对两个执行来源，解决候选冲突，再决定是否合并。</p></div><button onClick={() => { listing.refresh(); report.refresh(); }}>只读刷新</button></div>
    <Notice>{capabilities.error || (!supported && capabilities.data ? '当前后端缺少合并能力，请更新后端并重新检查连接。' : '')}</Notice><Notice>{action.error}</Notice>
    {attempt && <div className="notice" role="status"><p>{attempt.label}结果尚未确认。原参数和幂等键已保留，请确认原请求。</p><button disabled={action.busy || !props.active} onClick={() => void action.run(() => execute(pending.current!))}>用原键确认请求</button></div>}
    <section className="panel" aria-labelledby="merge-history"><h2 id="merge-history">提案记录</h2><p className="small muted">“未关闭”仅表示尚无决定；打开后才核对来源和验证。重开窗口只查询。</p><Notice>{listing.error}</Notice>
      <div className="table-scroll"><table><thead><tr><th>提案</th><th>目标 ← 来源</th><th>决定</th><th>创建时间</th></tr></thead><tbody>{rows(listing.data?.items).map(item => <tr key={str(item.proposal_id)}><td><button disabled={blocked} onClick={() => open(str(item.proposal_id))}>{short(item.proposal_id)}</button></td><td>{str(item.ours_branch)} ← {str(item.theirs_branch)}</td><td>{MERGE_STATUS[str(item.status)] ?? str(item.status)}</td><td>{date(item.created_at)}</td></tr>)}</tbody></table></div>
      {!listing.loading && !rows(listing.data?.items).length && <p>此页没有提案。可以创建提案，或按完整 ID 打开。</p>}
      <div className="form-end"><button disabled={!after} onClick={() => setAfter(undefined)}>回到最新</button><button disabled={!listing.data?.next_after_id} onClick={() => setAfter(str(listing.data?.next_after_id))}>下一页提案</button></div>
      <form className="inline-form" onSubmit={event => { event.preventDefault(); void action.run(async () => { if (!blocked) open(lookup); }); }}><label className="grow">提案 UUID<input value={lookup} onChange={event => setLookup(event.target.value)} spellCheck={false} /></label><button disabled={blocked || !lookup}>打开提案</button></form>
    </section>
    <NewProposal runId={props.runId} begin={begin} blocked={blocked} supported={supported} onCreated={value => open(str(value.proposal_id))} />
    <Notice>{report.error}</Notice>{report.loading && selected && <p role="status">正在核对提案…</p>}
    {report.data?.proposal_id === selected && <Proposal key={selected} view={report.data} active={props.active} blocked={blocked || Boolean(report.error)} begin={begin} refresh={report.refresh} openTimeline={props.openTimeline} newRunSupported={hasFeature(capabilities.data, 'merged_run_initialization')} />}
  </div>;
}

function NewProposal({ runId, begin, blocked, supported, onCreated }: { runId: string; begin: Begin; blocked: boolean; supported: boolean; onCreated: (value: JsonObject) => void }) {
  const [ours, setOurs] = useState(''), [theirs, setTheirs] = useState(''), [oursRun, setOursRun] = useState(runId), [theirsRun, setTheirsRun] = useState('');
  const [validator, setValidator] = useState(''), [selectedBase, setSelectedBase] = useState(''), [baseRun, setBaseRun] = useState('');
  const branches = useRead(() => supported ? call(window.agentgit.listBranches({ limit: 1000 })) : Promise.resolve({ items: [] } as JsonObject), [supported]);
  const available = rows(branches.data?.items), a = available.find(item => item.name === ours), b = available.find(item => item.name === theirs);
  const bases = useRead(() => a?.head_commit_id && b?.head_commit_id ? call(window.agentgit.getMergeBases({ ours: str(a.head_commit_id), theirs: str(b.head_commit_id) })) : Promise.resolve({} as JsonObject), [a?.head_commit_id, b?.head_commit_id]);
  const base = selectedBase || str(bases.data?.selected), choices = Array.isArray(bases.data?.bases) ? bases.data.bases : [];
  function side(label: string, name: string, setName: (value: string) => void, run: string, setRun: (value: string) => void, pin: JsonObject | undefined) {
    return <fieldset><legend>{label}</legend><label>{label}分支<select aria-label={`${label}分支`} value={name} onChange={event => { setName(event.target.value); setSelectedBase(''); setBaseRun(''); }}>{<option value="">选择分支</option>}{available.map(item => <option key={str(item.name)} value={str(item.name)}>{str(item.name)}</option>)}</select></label><label>{label}来源 Run UUID<input value={run} onChange={event => setRun(event.target.value)} spellCheck={false} /></label><p className="small">HEAD <code>{str(pin?.head_commit_id) || '—'}</code><br />分支版本 {String(pin?.version ?? '—')}</p></fieldset>;
  }
  return <details className="panel"><summary>创建合并提案</summary><p>固定两个分支当前 HEAD 与版本，Run 必须拥有相应历史路径。创建、编辑和批准均不启动执行。</p><fieldset disabled={blocked}><div className="repair-pair">{side('目标 ours', ours, setOurs, oursRun, setOursRun, a)}{side('来源 theirs', theirs, setTheirs, theirsRun, setTheirsRun, b)}</div>
    <button onClick={branches.refresh}>刷新分支</button><Notice>{branches.error || bases.error}</Notice>{branches.data?.has_more === true && <Notice>分支超过本页上限，请先在分支页核对。此处仅可选择当前已列分支。</Notice>}
    <label>共同祖先<select aria-label="共同祖先" value={base} disabled={!choices.length} onChange={event => { setSelectedBase(event.target.value); setBaseRun(''); }}><option value="">{bases.data?.status === 'unrelated' ? '没有共同祖先' : '多基点须明确选择'}</option>{choices.map(item => <option key={str(item)} value={str(item)}>{str(item)}</option>)}</select></label>
    <label>base 来源 Run UUID（仅执行归属有歧义时）<input value={baseRun} onChange={event => setBaseRun(event.target.value)} spellCheck={false} /></label>
    <label>冻结验收器摘要<input value={validator} onChange={event => setValidator(event.target.value)} spellCheck={false} maxLength={64} /></label>
    <div className="form-end"><button disabled={!a || !oursRun} onClick={() => { const input = { sourceRunId: oursRun, fromCommit: str(a?.head_commit_id), key: crypto.randomUUID() }; begin({ label: '冻结来源验收器', send: () => window.agentgit.freezeValidator(input), accepted: value => setValidator(str(value.validator_ref)) }); }}>冻结目标来源验收器</button>
      <button className="primary" disabled={!a || !b || ours === theirs || !oursRun || !theirsRun || !validator || !base || bases.loading || Boolean(bases.error)} onClick={() => {
        const pin = (branch: JsonObject, run: string): JsonObject => ({ name: str(branch.name), source_run_id: run, head: str(branch.head_commit_id), version: branch.version! });
        const request: JsonObject = { ours: pin(a!, oursRun), theirs: pin(b!, theirsRun), validator_ref: validator, selected_base: base, ...(baseRun ? { selected_base_run_id: baseRun } : {}) }, key = crypto.randomUUID();
        begin({ label: '创建合并提案', send: () => window.agentgit.createMergeProposal({ request, key }), accepted: onCreated });
      }}>创建固定来源提案</button></div></fieldset></details>;
}

function Proposal({ view, active, blocked, begin, refresh, openTimeline, newRunSupported }: { view: JsonObject; active: boolean; blocked: boolean; begin: Begin; refresh: () => void; openTimeline: (id: string) => void; newRunSupported: boolean }) {
  const [session, setSession] = useState<JsonObject | null>(null), [dirty, setDirty] = useState(false), [reason, setReason] = useState(''), [consent, setConsent] = useState(false);
  const reviewEpoch = useRef(0), live = useRef(active);
  live.current = active;
  useEffect(() => { reviewEpoch.current++; if (!active) { setSession(null); setConsent(false); } return () => { reviewEpoch.current++; }; }, [active, object(view.revision).revision_ref, view.validation_ref]);
  const revision = object(view.revision), candidate = object(view.candidate), manifest = object(view.manifest), gate = mergeReviewGate(view, session);
  const closed = ['merged', 'rejected'].includes(str(view.status)), version = () => mergeVersion(view, crypto.randomUUID());
  const noEdit = blocked || closed || view.status === 'stale';
  const change = (value: boolean) => { reviewEpoch.current++; setDirty(value); setSession(null); setConsent(false); };
  return <article aria-label="当前合并提案"><section className="panel"><h2>当前提案 · {MERGE_STATUS[str(view.status)] ?? str(view.status)}</h2><p><code>{str(view.proposal_id)}</code></p><dl className="facts"><div><dt>修订</dt><dd>{String(revision.revision ?? '—')}</dd></div><div><dt>修订摘要</dt><dd><code>{str(revision.revision_ref)}</code></dd></div><div><dt>候选摘要</dt><dd><code>{str(candidate.candidate_digest)}</code></dd></div></dl>
      {view.status === 'stale' && <Notice>来源 HEAD、版本或执行条件已变化。旧提案不能批准；请重新核对并创建提案。仍可明确拒绝当前修订。</Notice>}
      <Detail title="固定请求与来源证据" value={manifest} /><p className="small muted">完成声明、调用、用量与副作用只作来源审计。合并不继承轨迹、运行完成状态或执行授权。</p></section>
    <section className="panel"><h2>三方原文与来源</h2><div className="merge-origins">{rows(manifest.origins).map((origin, index) => <Source key={str(origin.commit_id)} origin={origin} name={['base', 'ours', 'theirs'][index]!} />)}</div></section>
    <section className="panel"><h2>候选与阻断项</h2>{rows(candidate.conflicts).length ? <ul className="merge-conflicts">{rows(candidate.conflicts).map((conflict, index) => <li key={index}><strong>{str(conflict.kind)}</strong><p>{Array.isArray(conflict.paths) ? conflict.paths.join('、') : ''}</p><Detail title="冲突证据与建议" value={conflict} /></li>)}</ul> : <p>当前候选没有结构化冲突，仍须重新验证并获取本人票据。</p>}
      <Detail title="计划、工作记忆与约束" value={candidate.state as JsonObject | null} />{rows(candidate.artifacts).map(file => <Detail key={str(file.path)} title={`${str(file.path)} · ${str(file.status)}`} value={str(file.text)} />)}
      <Detail title="本次验证原始结论" value={view.validation as JsonObject | null} />
    </section>
    <Editor key={str(revision.revision_ref)} candidate={candidate} blocked={noEdit} begin={begin} version={version} change={change} />
    <section className="panel"><h2>验证与决定</h2><p>保存新修订后旧验证与票据失效。历史票据仅供核对；批准须使用本次本人准备、确切绑定当前候选的票据。</p>{dirty && <Notice>草稿尚未保存。请保存新修订或放弃草稿后继续验证与审阅。</Notice>}
      <div className="form-end"><button disabled={noEdit || dirty} onClick={() => { const input = version(); setSession(null); setConsent(false); begin({ label: '验证当前修订', send: () => window.agentgit.validateMergeRevision(input) }); }}>验证当前修订</button>
        <button disabled={noEdit || dirty || !gate.validated} onClick={() => {
          const input = { ...version(), validationId: str(view.validation_id) }, proposalId = str(view.proposal_id), epoch = ++reviewEpoch.current; setSession(null); setConsent(false);
          begin({ label: '准备本人审阅票据', send: () => window.agentgit.beginMergeReview(input), accepted: receipt => {
            // 只激活本次明确请求取得的票据；随后通过只读端点核对，不从历史列表恢复批准能力。
            void call(window.agentgit.getMergeReview({ proposalId, reviewId: str(receipt.review_id) })).then(checked => {
              if (live.current && reviewEpoch.current === epoch && checked.ticket_ref === receipt.ticket_ref && checked.current === true) setSession({ ...checked, proposal_id: proposalId });
            }).catch(() => { if (reviewEpoch.current === epoch) setSession(null); });
          } });
        }}>核对并获取本人票据</button></div>
      {session && <Detail title="本次本人票据" value={session} open />}
      <label className="check"><input type="checkbox" checked={consent} disabled={blocked || dirty || !gate.approve} onChange={event => setConsent(event.target.checked)} />我已核对三方来源、候选及验证；批准只合并状态，不启动执行</label>
      <button className="primary" disabled={blocked || dirty || !gate.approve || !consent} onClick={() => {
        const input = { ...version(), reviewId: str(session?.review_id), ticketRef: str(session?.ticket_ref), candidateDigest: str(candidate.candidate_digest) }; setSession(null); setConsent(false);
        begin({ label: '批准确切候选', send: () => window.agentgit.approveMergeProposal(input) });
      }}>批准合并当前候选</button>
      <fieldset disabled={blocked || closed || dirty}><legend>拒绝当前修订</legend><label>拒绝原因<textarea value={reason} maxLength={4000} onChange={event => setReason(event.target.value)} /></label><button disabled={!reason.trim()} onClick={() => {
        const input = { ...version(), reason }; setSession(null); setConsent(false); begin({ label: '拒绝当前修订', send: () => window.agentgit.rejectMergeProposal(input) });
      }}>记录拒绝</button></fieldset>
      <Detail title="修订记录" value={rows(view.revisions)} />
      <ReviewHistory view={view} />{view.rejection && <Detail title="已记录的拒绝" value={object(view.rejection)} open />}
    </section>
    {view.approval && <AfterMerge key={str(object(view.approval).decision_id)} view={view} begin={begin} blocked={blocked || !newRunSupported} openTimeline={openTimeline} refresh={refresh} />}
  </article>;
}

function Source({ origin, name }: { origin: JsonObject; name: string }) {
  const [content, setContent] = useState<JsonObject>({}), action = useAction();
  const source = useRead(() => call(window.agentgit.getCommit({ commitId: str(origin.commit_id) })), [origin.commit_id]);
  const files = useRead(() => call(window.agentgit.getCommitArtifacts({ commitId: str(origin.commit_id), limit: 100 })), [origin.commit_id]);
  return <section><h3>{name}</h3><p><code>{str(origin.commit_id)}</code><br /><span>{origin.kind === 'approved_merge' ? '批准后的状态根 · 未执行' : <>Run <code>{str(origin.run_id)}</code></>}</span></p><Notice>{source.error || files.error || action.error}</Notice>
    <Detail title={`${name} 状态原文`} value={object(object(source.data?.snapshot).data)} /><Detail title={`${name} Commit 与环境`} value={source.data} />
    {rows(files.data?.items).map(file => <div key={str(file.id)}><p>{str(file.relative_path)} <code>{short(file.object_id)}</code></p><button disabled={action.busy} onClick={() => void action.run(async () => {
      let offset = 0, reference = '', total = 0; const parts: Uint8Array[] = [];
      do { const chunk = await call(window.agentgit.readArtifact({ artifactId: str(file.id), offset, limit: 65536 }));
        if (chunk.offset !== offset || (reference && chunk.objectRef !== reference) || chunk.totalBytes > 1048576) throw new Error('原文超过一 MiB 或读取版本变化，请导出核对');
        reference = chunk.objectRef; const bytes = Uint8Array.from(atob(chunk.base64), char => char.charCodeAt(0)); total += bytes.length; parts.push(bytes); offset = chunk.nextOffset ?? -1;
      } while (offset !== -1);
      const bytes = new Uint8Array(total); let at = 0; for (const part of parts) { bytes.set(part, at); at += part.length; }
      const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); setContent(previous => ({ ...previous, [str(file.id)]: text }));
    })}>读取 {str(file.relative_path)} 原文</button><button disabled={action.busy} onClick={() => void action.run(async () => { await call(window.agentgit.exportArtifact({ artifactId: str(file.id) })); })}>导出 {str(file.relative_path)}</button>{Object.hasOwn(content, str(file.id)) && <pre className="code" tabIndex={0}>{str(content[str(file.id)])}</pre>}</div>)}
    {files.data?.next_after_id && <p>产物超出本页。完整元数据见上方状态原文。</p>}
  </section>;
}

function Editor({ candidate, blocked, begin, version, change }: { candidate: JsonObject; blocked: boolean; begin: Begin; version: () => ReturnType<typeof mergeVersion>; change: (dirty: boolean) => void }) {
  const initial = object(candidate.edits);
  const [sources, setSources] = useState(pretty(initial.sources ?? {})), [sourceId, setSourceId] = useState(''), [choice, setChoice] = useState('ours');
  const [replace, setReplace] = useState(initial.replace_plan === true), [plan, setPlan] = useState(pretty(initial.plan ?? object(candidate.state).plan ?? null));
  const seedFile = (path: string) => rows(initial.artifacts).find(file => file.path === path), candidateFile = (path: string) => rows(candidate.artifacts).find(file => file.path === path);
  const [modes, setModes] = useState<Record<string, string>>(() => Object.fromEntries(['report.md', 'sources.json'].map(path => [path, str(seedFile(path)?.operation) || (seedFile(path) ? 'write' : 'auto')])));
  const [texts, setTexts] = useState<Record<string, string>>(() => Object.fromEntries(['report.md', 'sources.json'].map(path => [path, str(seedFile(path)?.text ?? candidateFile(path)?.text)])));
  const [dirty, setDirty] = useState(false), [error, setError] = useState('');
  // 修订摘要改变会重新挂载编辑器，轮询同一修订不覆盖草稿。
  useEffect(() => { change(false); }, []);
  function edit() { setDirty(true); change(true); setError(''); }
  return <section className="panel"><h2>完整修订候选</h2><p>每次保存替换整套编辑。自动模式沿用三方合并；显式来源与文件覆盖会进入新修订。单文件最多 1 MiB，完整请求（含 JSON 转义与其他编辑）也不得超过 1 MiB。</p><Notice>{error}</Notice><fieldset disabled={blocked}><legend>来源选择</legend><div className="repair-pair"><label>来源 Source ID<input value={sourceId} maxLength={200} onChange={event => setSourceId(event.target.value)} /></label><label>采用哪一侧<select aria-label="采用哪一侧" value={choice} onChange={event => setChoice(event.target.value)}>{['base', 'ours', 'theirs', 'delete'].map(value => <option key={value} value={value}>{value === 'delete' ? '删除该来源' : value}</option>)}</select></label></div><button disabled={!sourceId.trim()} onClick={() => { try { setSources(pretty({ ...parseObject(sources, '来源选择'), [sourceId]: choice })); edit(); } catch (failure) { setError((failure as Error).message); } }}>加入来源选择</button><label>完整来源选择 JSON<textarea aria-label="完整来源选择 JSON" className="mono" value={sources} onChange={event => { setSources(event.target.value); edit(); }} /></label>
    <label className="check"><input type="checkbox" checked={replace} onChange={event => { setReplace(event.target.checked); edit(); }} />明确替换候选计划</label>{replace && <label>完整 Plan JSON（null 表示删除计划）<textarea aria-label="完整 Plan JSON（null 表示删除计划）" className="mono" rows={8} value={plan} onChange={event => { setPlan(event.target.value); edit(); }} /></label>}
    {['report.md', 'sources.json'].map(path => <fieldset key={path}><legend>{path}</legend><label>{path} 编辑方式<select aria-label={`${path} 编辑方式`} value={modes[path]} onChange={event => { setModes(previous => ({ ...previous, [path]: event.target.value })); edit(); }}><option value="auto">自动三方合并</option><option value="write">写入完整文本</option><option value="delete">删除文件</option></select></label>{modes[path] === 'write' && <label>{path} 完整文本<textarea aria-label={`${path} 完整文本`} className="mono" rows={8} value={texts[path]} onChange={event => { setTexts(previous => ({ ...previous, [path]: event.target.value })); edit(); }} /></label>}</fieldset>)}
    <button disabled={!dirty} onClick={() => { try {
      const edits: JsonObject = { replace_plan: replace, sources: parseObject(sources, '来源选择'), artifacts: ['report.md', 'sources.json'].filter(path => modes[path] !== 'auto').map(path => modes[path] === 'delete' ? { path, operation: 'delete' } : { path, operation: 'write', text: texts[path]!, media_type: path === 'report.md' ? 'text/markdown' : 'application/json' }) };
      if (replace) edits.plan = plan.trim() === 'null' ? null : parseObject(plan, 'Plan');
      const input = { ...version(), edits }; begin({ label: '保存完整修订', send: () => window.agentgit.replaceMergeRevision(input) });
    } catch (failure) { setError((failure as Error).message); } }}>保存为新修订</button>
    {dirty && <button onClick={() => { setSources(pretty(initial.sources ?? {})); setReplace(initial.replace_plan === true); setPlan(pretty(initial.plan ?? object(candidate.state).plan ?? null)); setModes(Object.fromEntries(['report.md', 'sources.json'].map(path => [path, str(seedFile(path)?.operation) || (seedFile(path) ? 'write' : 'auto')]))); setTexts(Object.fromEntries(['report.md', 'sources.json'].map(path => [path, str(seedFile(path)?.text ?? candidateFile(path)?.text)]))); setDirty(false); change(false); setError(''); }}>放弃本地草稿</button>}
  </fieldset></section>;
}

function ReviewHistory({ view }: { view: JsonObject }) {
  const [before, setBefore] = useState('');
  const history = useRead(() => before ? call(window.agentgit.getMergeProposal({ proposalId: str(view.proposal_id), reviewBefore: before })) : Promise.resolve(view), [before, view]);
  return <details><summary>历史票据（只读）</summary><Notice>{history.error}</Notice>{rows(history.data?.reviews).map(ticket => <Detail key={str(ticket.review_id)} title={str(ticket.review_id)} value={ticket} />)}{history.data?.reviews_next_cursor && <button onClick={() => setBefore(str(history.data?.reviews_next_cursor))}>更早的票据</button>}{before && <button onClick={() => setBefore('')}>最新票据</button>}</details>;
}

function AfterMerge({ view, begin, blocked, openTimeline, refresh }: { view: JsonObject; begin: Begin; blocked: boolean; openTimeline: (id: string) => void; refresh: () => void }) {
  const approval = object(view.approval), [name, setName] = useState(''), [branch, setBranch] = useState<JsonObject | null>(null), [run, setRun] = useState<JsonObject | null>(null);
  const [config, setConfig] = useState(''), [configVersion, setConfigVersion] = useState('1'), [budget, setBudget] = useState(() => pretty(object(object(object(view.candidate).state).constraints).limits ?? {})), [consent, setConsent] = useState(false), [error, setError] = useState('');
  const commit = useRead(() => call(window.agentgit.getCommit({ commitId: str(approval.commit_id) })), [approval.commit_id]);
  const branches = useRead(() => call(window.agentgit.listBranches({ limit: 1000 })), [approval.commit_id]);
  return <section className="panel"><h2>已合并 · 后续执行须新建任务</h2><p>批准只发布双父 Commit。这个状态根没有 Recorded 轨迹；新任务从独立初始化检查点开始。</p><p>Commit <code>{str(approval.commit_id)}</code></p><Detail title="批准决定" value={approval} /><Detail title="双父 Commit 与合并状态" value={commit.data} /><Notice>{commit.error || branches.error || error}</Notice>
    <fieldset disabled={blocked || run !== null}><legend>1. 独立合并分支</legend><label>新分支名称<input value={name} maxLength={100} onChange={event => setName(event.target.value)} disabled={branch !== null} /></label><button disabled={branch !== null || !name.trim()} onClick={() => { const input = { approvalId: str(approval.decision_id), commitId: str(approval.commit_id), branchName: name, key: crypto.randomUUID() }; begin({ label: '建立独立合并分支', send: () => window.agentgit.forkMergedBranch(input), accepted: setBranch }); }}>建立独立合并分支</button>
      <label>或选择已建立的合并分支<select value={branch ? str(branch.name) : ''} onChange={event => { setBranch(rows(branches.data?.items).find(item => item.name === event.target.value) ?? null); setConsent(false); }}><option value="">选择相同批准根的分支</option>{rows(branches.data?.items).filter(item => item.head_commit_id === approval.commit_id && item.origin_kind === 'merge').map(item => <option key={str(item.name)} value={str(item.name)}>{str(item.name)}</option>)}</select></label><button onClick={branches.refresh}>查询已有分支</button></fieldset>
    {branch && <fieldset disabled={blocked || run !== null}><legend>2. 显式新任务与预算</legend><p>分支 <code>{str(branch.name)}</code> · 版本 {String(branch.version ?? '—')}。历史用量、完成、消息和授权不会进入新任务。</p><div className="repair-pair"><label>Agent 配置 UUID<input value={config} onChange={event => setConfig(event.target.value)} spellCheck={false} /></label><label>配置版本<input type="number" min={1} value={configVersion} onChange={event => setConfigVersion(event.target.value)} /></label></div><label>独立预算 JSON<textarea aria-label="独立预算 JSON" className="mono" value={budget} onChange={event => setBudget(event.target.value)} /></label><p className="small">预算由当前配置与合并约束共同限制；外部模型需要明确费用上限。已预填合并约束上限，请核对每项额度。</p><label className="check"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} />我选择此配置和预算，明确启动独立新任务</label>
      <button className="primary" disabled={!consent || !config || !configVersion} onClick={() => { try {
        const input = { branchName: str(branch.name), expectedHead: str(branch.head_commit_id), expectedBranchVersion: Number(branch.version), configId: identifier(config), configVersion: Number(configVersion), validatorRef: str(object(object(view.manifest).request).validator_ref), budget: parseObject(budget, '预算'), key: crypto.randomUUID() };
        begin({ label: '提交独立新任务', send: () => window.agentgit.submitMergedRun(input), accepted: value => { setRun(value); refresh(); } });
      } catch (failure) { setError((failure as Error).message); } }}>提交独立新任务</button></fieldset>}
    {run && <><h3>新任务已提交</h3><JsonView value={run} /><button onClick={() => openTimeline(str(run.run_id))}>查看新 Run 时间线</button><p className="small">提交收据不表示运行完成。Recorded 请显式选择此 Run 内已提交的初始化检查点。</p></>}
  </section>;
}
