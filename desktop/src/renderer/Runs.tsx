import React, { useRef, useState } from 'react';
import type { JsonObject } from '../shared/bridge';
import { call, date, Empty, JsonView, Notice, object, rows, short, Status, str, useAction, useRead, type PageProps } from './ui';

interface Submission { config: JsonObject; goal: JsonObject; branch: string; branchVersion: number; head: string | null; configId?: string; configVersion?: number; taskId?: string; keys: string[] }
export function Runs(props: PageProps) {
  const [after, setAfter] = useState<string | undefined>(), [newRun, setNewRun] = useState(false), [lookup, setLookup] = useState(''), [kind, setKind] = useState<'run' | 'job'>('run');
  const list = useRead(() => call(window.agentgit.listRuns({ ...(after ? { afterId: after } : {}), limit: 20 })), [after], 3000);
  const find = useAction();
  return <><div className="page-heading"><div><span className="eyebrow">RUNS</span><h1>运行</h1><p>从一次任务开始，保留每个决策与产物的来路。</p></div><button className="primary" onClick={() => setNewRun(value => !value)} aria-expanded={newRun}>{newRun ? '收起新建' : '新建运行'}</button></div>
    {newRun && <NewRun onAccepted={id => { list.refresh(); props.openTimeline(id); }} />}
    <div className="section-heading"><h2>项目运行记录</h2><button disabled={list.loading} onClick={list.refresh}>刷新</button></div><Notice>{list.error}</Notice>
    {list.loading ? <p role="status">正在读取运行…</p> : !rows(list.data?.items).length ? <Empty title={after ? '没有更多运行' : '还没有运行记录'}>创建固定离线研究任务，或按已有 Job / Run ID 打开。</Empty> : <div className="table-scroll"><table><thead><tr><th>目标 / Run</th><th>分支</th><th>模式</th><th>状态</th><th>创建时间</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{rows(list.data?.items).map(item => <tr key={str(item.id)}><td><strong className="table-goal">{str(object(item.goal).text) || '历史运行'}</strong><code title={str(item.id)}>{short(item.id)}</code>{item.job_id && <span className="small muted"> · Job {short(item.job_id)}</span>}</td><td><code>{str(item.branch_name)}</code></td><td>{str(item.mode) || 'standard'}</td><td><Status value={item.status} /></td><td className="nowrap small">{date(item.created_at)}</td><td><button className="text-button" onClick={() => props.openTimeline(str(item.id))}>查看时间线 →</button></td></tr>)}</tbody></table></div>}
    <div className="pagination"><span className="small muted">每页最多 20 项 · 按创建时间倒序</span><button disabled={!after} onClick={() => setAfter(undefined)}>回到最新</button><button disabled={!list.data?.next_after_id} onClick={() => setAfter(str(list.data?.next_after_id))}>更早运行</button></div>
    <form className="lookup panel" onSubmit={event => { event.preventDefault(); void find.run(async () => { const value = kind === 'job' ? await call(window.agentgit.getJob({ jobId: lookup.trim() })) : await call(window.agentgit.getRun({ runId: lookup.trim() })); props.openTimeline(str(kind === 'job' ? value.run_id : value.id)); }); }}><h2>按 ID 打开</h2><div className="inline-form"><label>类型<select value={kind} onChange={event => setKind(event.target.value as 'run' | 'job')}><option value="run">Run</option><option value="job">Job（查询最新 Run）</option></select></label><label className="grow">标识<input value={lookup} onChange={event => setLookup(event.target.value)} placeholder="粘贴 UUID" spellCheck={false} /></label><button disabled={find.busy || !lookup.trim()}>打开</button></div><Notice>{find.error}</Notice></form>
  </>;
}

function NewRun({ onAccepted }: { onAccepted: (id: string) => void }) {
  const capabilities = useRead(() => call(window.agentgit.capabilities()), []);
  const branches = useRead(() => call(window.agentgit.listBranches({})), []);
  const [goal, setGoal] = useState('比较 Aster、Birch、Cedar 的能力与约束，计算统计并整理产物。'), [branch, setBranch] = useState('main');
  const [execution, setExecution] = useState(true), [limits, setLimits] = useState(''), [frozen, setFrozen] = useState(false);
  const attempt = useRef<Submission | null>(null), action = useAction();
  const config = object(capabilities.data?.[execution ? 'execution_config' : 'default_config']);
  return <section className="panel new-run" aria-labelledby="new-run-title"><h2 id="new-run-title">新建离线研究运行</h2><p className="muted">使用 Mock 模型和合成资料。五工具模式会由后端在固定 Linux 容器中执行 Python 与 Shell。</p><Notice>{capabilities.error || branches.error}</Notice>
    <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
      if (!attempt.current) {
        const selected = rows(branches.data?.items).find(item => item.name === branch);
        if (!selected) throw new Error('请刷新并选择可用分支');
        const edited = limits.trim() ? JSON.parse(limits) as JsonObject : config.limits;
        attempt.current = { config: { ...config, limits: edited! }, goal: { schema_version: 1, text: goal }, branch,
          branchVersion: Number(selected.version), head: str(selected.head_commit_id) || null,
          keys: [crypto.randomUUID(), crypto.randomUUID(), crypto.randomUUID()] };
        setFrozen(true);
      }
      const saved = attempt.current;
      if (!saved.configId) { const value = await call(window.agentgit.createAgent({ config: saved.config, expectedVersion: 0, key: saved.keys[0]! })); saved.configId = str(value.config_id); saved.configVersion = Number(value.version); }
      if (!saved.taskId) { const value = await call(window.agentgit.createTask({ configId: saved.configId, configVersion: saved.configVersion!, goal: saved.goal, key: saved.keys[1]! })); saved.taskId = str(value.task_id); }
      const accepted = await call(window.agentgit.submitRun({ taskId: saved.taskId, configVersion: saved.configVersion!, branchName: saved.branch,
        expectedBranchVersion: saved.branchVersion, expectedHead: saved.head, key: saved.keys[2]! }));
      onAccepted(str(accepted.run_id));
    }); }}><label>研究目标<textarea rows={3} maxLength={4000} value={goal} disabled={frozen} onChange={event => setGoal(event.target.value)} /></label>
      <div className="inline-form"><label className="grow">目标分支<select value={branch} disabled={frozen} onChange={event => setBranch(event.target.value)}>{rows(branches.data?.items).map(item => <option key={str(item.name)}>{str(item.name)}</option>)}</select></label><label className="check"><input type="checkbox" checked={execution} disabled={frozen} onChange={event => setExecution(event.target.checked)} />启用固定五工具</label></div>
      <details><summary>完整配置与预算</summary><p className="muted small">仅支持后端当前公开的模型与受控工具；预算为完整整数契约，不可超过配置上限。</p><JsonView value={config} /><label>预算 JSON（留空使用上方默认 limits）<textarea className="mono" rows={5} value={limits} disabled={frozen} onChange={event => setLimits(event.target.value)} placeholder={JSON.stringify(config.limits, null, 2)} spellCheck={false} /></label></details>
      <div className="form-end"><span className="muted small">{frozen ? `保留原动作键 · Task ${attempt.current?.taskId ?? '尚未受理'}` : '当前分支 HEAD/version 将作为提交前置条件'}</span><button className="primary" disabled={action.busy || !goal.trim() || !capabilities.data || !branches.data}>{action.busy ? '提交中…' : frozen ? '使用原幂等键继续' : '提交运行'}</button></div><Notice>{action.error}</Notice>
    </form></section>;
}
