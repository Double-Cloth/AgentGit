import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import type { ConnectionState } from '../shared/bridge';
import type { CandidateExperimentPreset } from '../shared/diagnosis';
import type { CandidateRepairPreset } from '../shared/repair';
import { call, err, Notice, useAction, type EvidenceTarget } from './ui';
import { Runs } from './Runs';
const Timeline = lazy(() => import('./Timeline'));
const Branch = lazy(() => import('./Branch'));
const Diff = lazy(() => import('./Diff'));
const RepairLab = lazy(() => import('./RepairLab'));
const MergeReview = lazy(() => import('./MergeReview'));
const PAGES = [{ id: 'runs', text: '运行' }, { id: 'timeline', text: '时间线' }, { id: 'branch', text: '分支实验' }, { id: 'diff', text: '差异' }, { id: 'repair', text: '修复实验室' }, { id: 'merge', text: '合并审阅' }] as const;
type Page = typeof PAGES[number]['id'];

export function App() {
  const [page, setPage] = useState<Page>('runs'), [connection, setConnection] = useState<ConnectionState | null>(null);
  const [settings, setSettings] = useState(false), [problem, setProblem] = useState('');
  const [collaborationId, setCollaborationId] = useState('');
  const [runId, setRunId] = useState(''), [commit, setCommit] = useState('');
  const [eventFocus, setEventFocus] = useState<EvidenceTarget | null>(null), [diagnosisRunId, setDiagnosisRunId] = useState('');
  const [experimentPreset, setExperimentPreset] = useState<CandidateExperimentPreset | null>(null);
  const [repairPreset, setRepairPreset] = useState<CandidateRepairPreset | null>(null);
  const [confirmedWorkspace, setConfirmedWorkspace] = useState('');
  useEffect(() => {
    let active = true, first = true, timer: ReturnType<typeof setTimeout>;
    async function check() {
      try {
        const value = await call(window.agentgit.getConnection());
        if (active) { setConnection(value); setProblem(''); if (first && (!value.repositoryId || value.identity !== 'accepted')) setSettings(true); }
      } catch (failure) { if (active) setProblem(err(failure)); }
      finally { first = false; if (active) timer = setTimeout(() => void check(), 5000); }
    }
    void check();
    return () => { active = false; clearTimeout(timer); };
  }, []);
  useEffect(() => () => window.agentgit.cancelReads(), [page, connection?.repositoryId, connection?.baseUrl]);
  const ready = connection?.identity === 'accepted' && connection.database === 'ready' && connection.compatibility === 'compatible';
  const workspace = connection?.repositoryId ? connection.repositoryId + ':' + connection.baseUrl : '';
  useEffect(() => { if (ready && workspace) setConfirmedWorkspace(workspace); }, [ready, workspace]);
  const retainWorkspace = Boolean(workspace && (ready || confirmedWorkspace === workspace));
  function changeConnection(value: ConnectionState) { setCollaborationId(''); setConnection(value); setRunId(''); setCommit(''); setEventFocus(null); setDiagnosisRunId(''); setExperimentPreset(null); setRepairPreset(null); }
  const props = { collaborationId, openCollaboration: (id: string) => { setCollaborationId(id); setPage('runs'); }, runId, setRunId, selectedCommit: commit, setSelectedCommit: setCommit,
    openTimeline: (id: string) => { setRunId(id); setEventFocus(null); setPage('timeline'); },
    openDiagnosis: (id: string) => { setDiagnosisRunId(id); setPage('diff'); }, eventFocus,
    openRepair: (id: string, selected = '') => { setRunId(id); setCommit(selected); setRepairPreset(null); setPage('repair'); },
    openBranch: (id: string, selected: string) => { setRunId(id); setCommit(selected); setExperimentPreset(null); setPage('branch'); }, experimentPreset };
  return <><a href="#main" className="skip">跳到主要内容</a><header className="topbar"><div className="shell topbar-inner">
    <span className="wordmark">AgentGit</span><nav aria-label="主要页面">{PAGES.map(item => <button key={item.id} className={`nav-button ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined} onClick={() => { if (item.id === 'repair') setRepairPreset(null); setPage(item.id); }}>{item.text}</button>)}</nav>
    <button className="connection-button" onClick={() => setSettings(value => !value)} aria-expanded={settings}><span className={`connection-dot ${ready ? 'connected' : ''}`} aria-hidden="true" />{ready ? 'API 已连接' : '连接设置'}</button>
  </div></header><div className="shell contextbar"><span>当前项目</span><code>{connection?.repositoryId ?? '尚未选择'}</code><button className="text-button" onClick={() => setSettings(true)}>切换项目</button><span className="context-end">本机 · Windows x64</span></div>
    <main id="main" className="shell" tabIndex={-1}><Notice>{problem || (connection && !ready ? connection.message : '')}</Notice>
      {settings && <Connection key={connection?.baseUrl ?? 'initial'} state={connection} onChange={changeConnection} onClose={() => setSettings(false)} />}
      {!retainWorkspace && <section className="welcome"><span className="eyebrow">执行历史 · 隔离分支 · 可审计回放</span><h1>让每次执行有据可查。</h1><p>连接本机 API 后选择项目，查看运行、追溯检查点，并比较独立分支的结果。</p><p className="muted">桌面只展示和管理任务。Worker 与固定容器是否就绪，以实际任务证据为准。</p>{!settings && <button className="primary" onClick={() => setSettings(true)}>配置连接与项目</button>}</section>}
      {retainWorkspace && <fieldset className="workspace" disabled={!ready} aria-label="当前项目工作区" key={workspace}><Suspense fallback={<p role="status">正在打开页面…</p>}>
          {page === 'runs' ? <Runs {...props} active={ready} /> : page === 'timeline' ? <Timeline {...props} /> : page === 'branch' ? <Branch {...props} /> : page === 'merge' ? <MergeReview {...props} active={ready} /> : page === 'repair' ? <RepairLab {...props} active={ready} preset={repairPreset} /> : <Diff diagnosisRunId={diagnosisRunId} openEvent={target => { setEventFocus(target); setRunId(target.runId); setPage('timeline'); }} openExperiment={preset => { setRunId(preset.sourceRunId); setCommit(preset.fromCommit); setExperimentPreset(preset); setPage('branch'); }} openRepair={preset => { setRunId(preset.sourceRunId); setCommit(preset.fromCommit); setRepairPreset(preset); setPage('repair'); }} />}
        </Suspense></fieldset>}
    </main><footer className="shell footer"><span>AgentGit {__AGENTGIT_DESKTOP_VERSION__}</span><span>状态可恢复、历史可验证与修复有效，分别判断。</span></footer></>;
}

function Connection({ state, onChange, onClose }: { state: ConnectionState | null; onChange: (state: ConnectionState) => void; onClose: () => void }) {
  const [url, setUrl] = useState(state?.baseUrl ?? 'http://127.0.0.1:8000'), [repository, setRepository] = useState(state?.repositoryId ?? ''), [name, setName] = useState('');
  const action = useAction(), createKey = useRef(crypto.randomUUID());
  const [creation, setCreation] = useState<{ name: string; id: string | null } | null>(null);
  const available = state?.identity === 'accepted' && state.database === 'ready' && state.compatibility === 'compatible';
  return <section className="settings panel" aria-labelledby="settings-title"><div className="section-heading"><h2 id="settings-title">连接与项目</h2><button className="text-button" onClick={onClose}>收起</button></div>
    <p>API 地址仅支持回环 HTTP。身份由启动桌面前设置的 <code>AGENTGIT_API_TOKEN</code> 提供。</p>
    <div className="inline-form"><label className="grow">本机 API 地址<input value={url} onChange={event => setUrl(event.target.value)} placeholder="http://127.0.0.1:8000" spellCheck={false} /></label><button disabled={action.busy} onClick={() => void action.run(async () => onChange(await call(window.agentgit.configureConnection({ baseUrl: url }))))}>连接</button><button disabled={action.busy} onClick={() => void action.run(async () => onChange(await call(window.agentgit.getConnection())))}>重新检查</button></div>
    {state && <><p className="connection-message" role="status">{state.message}</p><dl className="facts"><div><dt>API</dt><dd>{state.api}</dd></div><div><dt>数据库</dt><dd>{state.database}</dd></div><div><dt>身份</dt><dd>{state.identity}</dd></div><div><dt>版本</dt><dd>{state.backendVersion ?? '未知'}</dd></div><div><dt>Worker / 容器</dt><dd>未知 / 未探测</dd></div></dl></>}
    <div className="settings-projects"><form onSubmit={event => { event.preventDefault(); void action.run(async () => onChange(await call(window.agentgit.selectRepository({ repositoryId: repository.trim() })))); }}><label>已有项目 ID<input value={repository} onChange={event => setRepository(event.target.value)} placeholder="粘贴项目 UUID" spellCheck={false} /></label><button disabled={!available || action.busy || !repository.trim()}>选择项目</button></form>
    <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
      const submitted = creation ?? { name: name.trim(), id: null }; setCreation(submitted);
      const id = submitted.id ?? String((await call(window.agentgit.createRepository({ name: submitted.name, key: createKey.current }))).repository_id);
      setCreation({ ...submitted, id }); setRepository(id);
      onChange(await call(window.agentgit.selectRepository({ repositoryId: id })));
      setCreation(null); setName(''); createKey.current = crypto.randomUUID();
    }); }}><label>新项目名称<input maxLength={200} value={creation?.name ?? name} disabled={creation !== null} onChange={event => setName(event.target.value)} placeholder="例如：框架研究" /></label><button disabled={!available || action.busy || !(creation?.name ?? name).trim()}>{creation ? '继续原创建操作' : '创建并选择'}</button></form></div>
    <Notice>{action.error}</Notice><p className="muted small">创建结果未确认时保留原幂等键；重新打开窗口只查询，不自动重发创建。退出桌面不会停止后端任务。</p>
    <LicenseNotices />
  </section>;
}

function LicenseNotices() {
  const [text, setText] = useState<string | null>(null), [error, setError] = useState('');
  return <details onToggle={event => {
    if (event.currentTarget.open && text === null) void import('./assets/THIRD_PARTY_NOTICES.txt?raw')
      .then(value => setText(value.default)).catch(() => setError('授权说明读取失败，请检查安装包资源'));
  }}><summary>版本与开源授权</summary><p className="small muted">AgentGit {__AGENTGIT_DESKTOP_VERSION__} · Windows x64</p><Notice>{error}</Notice>
    {text === null ? <p role="status">正在读取包内授权说明…</p> : <pre className="code license-text" tabIndex={0}>{text}</pre>}
  </details>;
}
