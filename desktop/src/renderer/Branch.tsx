import React, { useRef, useState } from 'react';
import type { JsonObject } from '../shared/bridge';
import { call, Empty, JsonView, Notice, object, rows, short, str, useAction, useRead, type PageProps } from './ui';

type Mode = 'recorded' | 'live' | 'fork';
export default function Branch(props: PageProps) {
  const [mode, setMode] = useState<Mode>('recorded'), [source, setSource] = useState(props.runId), [from, setFrom] = useState(props.selectedCommit);
  const [until, setUntil] = useState(''), [name, setName] = useState(''), [overrides, setOverrides] = useState('{}'), [budget, setBudget] = useState('');
  const [jobId, setJobId] = useState(''), [accepted, setAccepted] = useState<JsonObject | null>(null), [frozen, setFrozen] = useState(false);
  const attempt = useRef<{ mode: Mode; request: JsonObject; key: string } | null>(null), action = useAction();
  const runs = useRead(() => call(window.agentgit.listRuns({ limit: 100 })), []);
  const run = useRead(() => source ? call(window.agentgit.getRun({ runId: source })) : Promise.resolve({} as JsonObject), [source]);
  const commits = useRead(() => source && run.data?.branch_name ? call(window.agentgit.listCommits({ branchName: str(run.data.branch_name), limit: 100 })) : Promise.resolve({} as JsonObject), [run.data?.branch_name]);
  const report = useRead(() => jobId ? call(window.agentgit.getReplay({ jobId })) : Promise.resolve({} as JsonObject), [jobId], jobId ? 3000 : 0);
  return <><div className="page-heading"><div><span className="eyebrow">BRANCH EXPERIMENT</span><h1>分支实验</h1><p>选择安全检查点，明确历史验证与独立重执行的边界。</p></div></div>
    <div className="branch-layout"><section className="panel"><h2>建立独立分支</h2><div className="mode-options" role="radiogroup" aria-label="分支模式">{([['recorded', 'Recorded', '验证已记录历史'], ['live', 'Live', '重新执行剩余计划'], ['fork', 'Fork', '只建立安全分支']] as const).map(([id, title, description]) => <label key={id} className={mode === id ? 'selected' : ''}><input type="radio" name="mode" value={id} checked={mode === id} disabled={frozen} onChange={() => setMode(id)} /><strong>{title}</strong><span>{description}</span></label>)}</div>
      <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
        if (!attempt.current) {
          const request: JsonObject = { schema_version: 1, source_run_id: source.trim(), from_commit: from.trim(), expected_version: 0 };
          if (mode !== 'fork') { request.mode = mode; request.branch_name = name.trim(); }
          if (mode === 'recorded') request.until_commit = until.trim();
          if (mode === 'live') {
            request.overrides = JSON.parse(overrides) as JsonObject;
            if (budget.trim()) request.budget = JSON.parse(budget) as JsonObject;
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
        {mode === 'live' && <><label>干预 overrides JSON<textarea rows={8} className="mono" value={overrides} disabled={frozen} onChange={event => setOverrides(event.target.value)} spellCheck={false} /></label><p className="small muted">支持完整 config、plan、artifacts 与类型化 execution。文件仅为工作区安全相对路径；Shell 命令写入版本化文件，不调用宿主 Shell。</p><label>独立预算 JSON（可选）<textarea rows={5} className="mono" value={budget} disabled={frozen} onChange={event => setBudget(event.target.value)} placeholder="留空使用新配置上限；提供时必须是完整 BudgetLimits" spellCheck={false} /></label></>}
        <div className="form-end"><span className="small muted">{frozen ? '已固定请求，重试复用原幂等键' : '原历史不变，新分支独立保存'}</span><button className="primary" disabled={action.busy || Boolean(accepted) || !source.trim() || !from.trim() || !name.trim() || mode === 'recorded' && !until.trim()}>{action.busy ? '提交中…' : accepted ? '已受理' : frozen ? '继续原请求' : mode === 'fork' ? '创建分支' : `提交 ${mode === 'recorded' ? 'Recorded' : 'Live'}`}</button></div>
        <Notice>{action.error}</Notice>
      </form>
    </section><aside className="branch-aside"><section className="explanation"><span className="eyebrow">{mode.toUpperCase()}</span><h2>{mode === 'recorded' ? '验证发生过的执行。' : mode === 'live' ? '从独立输入重新执行。' : '保留一个安全起点。'}</h2><p>{mode === 'recorded' ? '只使用所选范围的完整录制，逐检查点重算。不调用模型或工具，不接收配置、预算与执行干预。缺少证据会失败。' : mode === 'live' ? '创建新配置、Task、Job、Run 与工作区。使用独立预算，重新取得授权；旧审批、临时文件和进程不被继承。' : '只在安全检查点建立新分支，不自动执行或验证。未知副作用、未结审批等检查由后端强制执行。'}</p><p className="small muted">{mode === 'recorded' ? 'verified=true 只表示该范围的历史验证通过。逻辑终点可能仍是未完成或未知。' : mode === 'live' ? 'Live 不承诺复现旧输出。客观差异可查，修复有效仍需后续冻结验证器与独立复测。' : '分支创建成功不表示任务已重新执行。'}</p></section>
    <section className="source-summary"><h2>来源摘要</h2><Notice>{runs.error || run.error || commits.error}</Notice>{source ? <dl className="facts"><div><dt>Run</dt><dd><code>{short(source)}</code></dd></div><div><dt>分支</dt><dd>{str(run.data?.branch_name) || '待查询'}</dd></div><div><dt>状态</dt><dd>{str(run.data?.status) || '待查询'}</dd></div><div><dt>HEAD</dt><dd><code>{short(run.data?.checkpoint_commit_id)}</code></dd></div></dl> : <p className="muted">先选择来源运行。</p>}<p className="small muted">候选列表最多 100 项；起止点归属与安全性以提交时校验为准。</p></section>
    {accepted && <section className="panel"><h2>已受理</h2><JsonView value={accepted} />{jobId && <><Notice>{report.error}</Notice><h3>最新 Job / Replay 状态</h3><JsonView value={report.data} /></>}{(report.data?.run_id || accepted.run_id) && <button onClick={() => props.openTimeline(str(report.data?.run_id || accepted.run_id))}>打开新运行时间线 →</button>}</section>}
    {!accepted && <Empty title="尚未提交">选择模式后提交。重新连接或打开页面只查询现有任务。</Empty>}
    </aside></div></>;
}
