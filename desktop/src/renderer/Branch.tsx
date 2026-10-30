import React, { lazy, Suspense, useRef, useState } from 'react';
import { hasFeature } from '../shared/experiments';
import type { JsonObject } from '../shared/bridge';
import { jsonObject, liveBudget, liveIsExternal } from '../shared/model-config';
import { ReplayUsage } from './BudgetSummary';
import { fixedProgramKind, fixedProgramTitle } from '../shared/fixed-program';
import { call, Empty, JsonView, Notice, object, rows, short, str, useAction, useRead, type PageProps } from './ui';

type Mode = 'recorded' | 'live' | 'fork';
const Experiments = lazy(() => import('./Experiments'));
export default function Branch(props: PageProps) {
  const [view, setView] = useState(props.experimentPreset ? 'experiment' : 'single'), [opened, setOpened] = useState(Boolean(props.experimentPreset));
  const capabilities = useRead(() => call(window.agentgit.capabilities()), [], 5000);
  const supported = hasFeature(capabilities.data, 'controlled_experiments') && hasFeature(capabilities.data, 'experiment_statistics');
  return <><div className="page-heading"><div><span className="eyebrow">BRANCH EXPERIMENT</span><h1>分支实验</h1><p>选择安全检查点，明确历史验证与独立重执行的边界。</p></div></div>
    <div className="tabs" role="group" aria-label="实验入口"><button aria-pressed={view === 'single'} className={view === 'single' ? 'active' : ''} onClick={() => setView('single')}>单次分支</button><button disabled={!supported} aria-pressed={view === 'experiment'} className={view === 'experiment' ? 'active' : ''} onClick={() => { setOpened(true); setView('experiment'); }}>受控 A/B 实验</button></div>
    {!supported && <p className="small muted">当前后端尚未确认支持受控实验与统计。单次分支仍可使用。</p>}
    <div hidden={view !== 'single'}><SingleBranch {...props} active={view === 'single'} /></div>
    {opened && <div hidden={view !== 'experiment'}><Suspense fallback={<p role="status">正在载入实验表单…</p>}><Experiments {...props} active={view === 'experiment' && supported} capabilities={capabilities.data} /></Suspense></div>}
  </>;
}
function SingleBranch(props: PageProps & { active: boolean }) {
  const [mode, setMode] = useState<Mode>('recorded'), [source, setSource] = useState(props.runId), [from, setFrom] = useState(props.selectedCommit);
  const [until, setUntil] = useState(''), [name, setName] = useState(''), [overrides, setOverrides] = useState('{}'), [budget, setBudget] = useState('');
  const [jobId, setJobId] = useState(''), [accepted, setAccepted] = useState<JsonObject | null>(null), [frozen, setFrozen] = useState(false);
  const [money, setMoney] = useState('');
  const attempt = useRef<{ mode: Mode; request: JsonObject; key: string } | null>(null), action = useAction();
  const runs = useRead(() => call(window.agentgit.listRuns({ limit: 100 })), []);
  const run = useRead(() => source ? call(window.agentgit.getRun({ runId: source })) : Promise.resolve({} as JsonObject), [source]);
  const commits = useRead(() => source && run.data?.branch_name ? call(window.agentgit.listCommits({ branchName: str(run.data.branch_name), limit: 100 })) : Promise.resolve({} as JsonObject), [run.data?.branch_name]);
  const report = useRead(() => jobId && props.active ? call(window.agentgit.getReplay({ jobId })) : Promise.resolve(accepted ?? {} as JsonObject), [jobId, props.active], jobId && props.active ? 3000 : 0, jobId);
  const externalLive = mode === 'live' && liveIsExternal(run.data, overrides);
  const fixedProgram = Boolean(fixedProgramKind(run.data));
  const sourceReady = str(run.data?.id).toLowerCase() === source.trim().toLowerCase() && Boolean(source.trim());
  return <>
    <div className="branch-layout"><section className="panel"><h2>建立独立分支</h2><div className="mode-options" role="radiogroup" aria-label="分支模式">{([['recorded', 'Recorded', '验证已记录历史'], ['live', 'Live', '重新执行剩余计划'], ['fork', 'Fork', '只建立安全分支']] as const).map(([id, title, description]) => <label key={id} className={mode === id ? 'selected' : ''}><input type="radio" name="mode" value={id} checked={mode === id} disabled={frozen || fixedProgram} onChange={() => setMode(id)} /><strong>{title}</strong><span>{description}</span></label>)}</div>
      {fixedProgram && <div className="explanation" role="status"><h3>{fixedProgramTitle(run.data!)}</h3><p>此任务使用固定程序，分支与回放目前由后端操作者提交。本表单仅用于研究任务，不会替换图、工具或权限。</p><button onClick={() => props.openTimeline(source)}>查看来源时间线</button></div>}
      <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
        if (!sourceReady) throw new Error('请等待来源运行核对完成');
        if (fixedProgram) throw new Error('固定程序的分支与回放须由后端操作者提交');
        if (!attempt.current) {
          const request: JsonObject = { schema_version: 1, source_run_id: source.trim(), from_commit: from.trim(), expected_version: 0 };
          if (mode !== 'fork') { request.mode = mode; request.branch_name = name.trim(); }
          if (mode === 'recorded') request.until_commit = until.trim();
          if (mode === 'live') {
            if (str(run.data?.id).toLowerCase() !== source.trim().toLowerCase()) throw new Error('请等待来源运行核对完成后再提交 Live');
            const parsed = jsonObject(overrides, '干预');
            request.overrides = parsed;
            if (externalLive) request.budget = liveBudget(run.data, parsed, budget, money);
            else if (budget.trim()) request.budget = jsonObject(budget, '独立预算');
          }
          if (mode === 'fork') request.name = name.trim();
          attempt.current = { mode, request, key: crypto.randomUUID() }; setFrozen(true);
        }
        const saved = attempt.current;
        const result = saved.mode === 'fork' ? await call(window.agentgit.createBranch({ sourceRunId: str(saved.request.source_run_id),
          fromCommit: str(saved.request.from_commit), branchName: str(saved.request.name), key: saved.key })) : await call(window.agentgit.replay({ request: saved.request, key: saved.key }));
        setAccepted(result); if (saved.mode !== 'fork') setJobId(str(result.job_id));
      }); }}>
        <label>来源 Run<input value={source} onChange={event => { setSource(event.target.value); setFrom(''); setUntil(''); }} disabled={frozen} list="source-runs" placeholder="来源 Run UUID" spellCheck={false} /></label><datalist id="source-runs">{rows(runs.data?.items).map(item => <option value={str(item.id)} key={str(item.id)}>{str(object(item.goal).text) || str(item.branch_name)} · {str(item.mode)}</option>)}</datalist>
        <label>安全起点 Commit<input value={from} disabled={frozen} onChange={event => setFrom(event.target.value)} list="source-commits" placeholder="完整 64 位摘要" spellCheck={false} /></label>
        {mode === 'recorded' && <label>验证终点 Commit<input value={until} disabled={frozen} onChange={event => setUntil(event.target.value)} list="source-commits" placeholder="同一来源 Run 上的终点（可等于起点）" spellCheck={false} /></label>}
        <datalist id="source-commits">{rows(commits.data?.items).map(item => <option value={str(item.commit_id)} key={str(item.commit_id)} />)}</datalist>
        <label>新分支名称<input maxLength={100} value={name} disabled={frozen} onChange={event => setName(event.target.value)} placeholder={mode === 'recorded' ? '例如：recorded-check' : mode === 'live' ? '例如：live-a' : '例如：research-fork'} /></label>
        {mode === 'live' && <><label>干预 overrides JSON<textarea rows={8} className="mono" value={overrides} disabled={frozen} onChange={event => setOverrides(event.target.value)} spellCheck={false} /></label><p className="small muted">支持完整 config、plan、artifacts 与类型化 execution。文件仅为工作区安全相对路径；Shell 命令写入版本化文件，不调用宿主 Shell。</p>{externalLive && <div className="model-options"><p>本次 Live 使用外部模型，会产生新调用。任务上下文发送到供应商，账户访问未验证；请明确本次独立费用预算。</p><label>本次 Live 费用上限（USD）<input value={money} disabled={frozen} inputMode="decimal" required onChange={event => setMoney(event.target.value)} placeholder="例如 0.50" aria-describedby="live-money-hint" /></label><p id="live-money-hint" className="small muted">最多 6 位小数；只可收紧来源或新配置上限。费用是价格表估计，不是供应商账单。</p></div>}<label>独立预算 JSON（可选）<textarea rows={5} className="mono" value={budget} disabled={frozen} onChange={event => setBudget(event.target.value)} placeholder={externalLive ? '留空复制来源/新配置 limits，并使用上方 USD 上限' : '留空使用新配置上限；提供时必须是完整 BudgetLimits'} spellCheck={false} /></label></>}
        <div className="form-end"><span className="small muted">{frozen ? '已固定请求，重试复用原幂等键' : mode === 'live' && !sourceReady ? '正在核对来源运行…' : '原历史不变，新分支独立保存'}</span><button className="primary" disabled={action.busy || Boolean(accepted) || fixedProgram || !sourceReady || !source.trim() || !from.trim() || !name.trim() || mode === 'recorded' && !until.trim() || mode === 'live' && !sourceReady || externalLive && !money.trim()}>{action.busy ? '提交中…' : accepted ? '已受理' : frozen ? '继续原请求' : mode === 'fork' ? '创建分支' : `提交 ${mode === 'recorded' ? 'Recorded' : 'Live'}`}</button></div>
        <Notice>{action.error}</Notice>
      </form>
    </section><aside className="branch-aside"><section className="explanation"><span className="eyebrow">{mode.toUpperCase()}</span><h2>{mode === 'recorded' ? '验证发生过的执行。' : mode === 'live' ? '从独立输入重新执行。' : '保留一个安全起点。'}</h2><p>{mode === 'recorded' ? '只使用所选范围的完整录制，逐检查点重算。不调用模型或工具，不接收配置、预算与执行干预。缺少证据会失败。' : mode === 'live' ? '创建新配置、Task、Job、Run 与工作区。使用独立预算，重新取得授权；旧审批、临时文件和进程不被继承。' : '只在安全检查点建立新分支，不自动执行或验证。未知副作用、未结审批等检查由后端强制执行。'}</p><p className="small muted">{mode === 'recorded' ? 'verified=true 只表示该范围的历史验证通过。逻辑终点可能仍是未完成或未知。' : mode === 'live' ? 'Live 不承诺复现旧输出。客观差异可查，修复有效仍需后续冻结验证器与独立复测。' : '分支创建成功不表示任务已重新执行。'}</p></section>
    <section className="source-summary"><h2>来源摘要</h2><Notice>{runs.error || run.error || commits.error}</Notice>{source ? <dl className="facts"><div><dt>Run</dt><dd><code>{short(source)}</code></dd></div><div><dt>分支</dt><dd>{str(run.data?.branch_name) || '待查询'}</dd></div><div><dt>状态</dt><dd>{str(run.data?.status) || '待查询'}</dd></div><div><dt>HEAD</dt><dd><code>{short(run.data?.checkpoint_commit_id)}</code></dd></div></dl> : <p className="muted">先选择来源运行。</p>}<p className="small muted">候选列表最多 100 项；起止点归属与安全性以提交时校验为准。</p></section>
    {accepted && <section className="panel"><h2>已受理</h2><JsonView value={accepted} />{jobId && <><Notice>{report.error}</Notice><ReplayUsage report={object(report.data?.replay)} /><h3>最新 Job / Replay 状态原文</h3><JsonView value={report.data} /></>}{(report.data?.run_id || accepted.run_id) && <button onClick={() => props.openTimeline(str(report.data?.run_id || accepted.run_id))}>打开新运行时间线 →</button>}</section>}
    {!accepted && <Empty title="尚未提交">选择模式后提交。重新连接或打开页面只查询现有任务。</Empty>}
    </aside></div></>;
}
