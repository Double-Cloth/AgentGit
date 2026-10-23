import React, { useRef, useState } from 'react';
import type { JsonObject } from '../shared/bridge';
import { buildRunConfig, modelChoices, parseUsd, usd } from '../shared/model-config';
import { call, date, Empty, JsonView, Notice, object, rows, short, Status, str, useAction, useRead, type PageProps } from './ui';

interface Submission { config: JsonObject; goal: JsonObject; branch: string; branchVersion: number; head: string | null; expectedConfigVersion?: number; configId?: string; configVersion?: number; taskId?: string; keys: string[] }
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
  const [model, setModel] = useState('mock-research/v1'), [money, setMoney] = useState('');
  const attempt = useRef<Submission | null>(null), action = useAction();
  const config = object(capabilities.data?.[execution ? 'execution_config' : 'default_config']);
  const choices = modelChoices(capabilities.data), choice = choices.find(item => item.id === model);
  let moneyError = '';
  if (choice?.external && money.trim()) { try { parseUsd(money); } catch (error) { moneyError = error instanceof Error ? error.message : '金额无效'; } }
  return <section className="panel new-run" aria-labelledby="new-run-title"><h2 id="new-run-title">新建研究运行</h2><p className="muted">使用合成资料和受控工具。五工具模式由后端固定 Linux 容器执行 Python 与 Shell。</p><Notice>{capabilities.error || branches.error}</Notice>
    <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
      if (!attempt.current) {
        const selected = rows(branches.data?.items).find(item => item.name === branch);
        if (!selected) throw new Error('请刷新并选择可用分支');
        if (!choice) throw new Error('当前后端未公开所选模型，请刷新能力描述');
        const prepared = buildRunConfig(config, choice, limits, money);
        attempt.current = { config: prepared, goal: { schema_version: 1, text: goal }, branch,
          branchVersion: Number(selected.version), head: str(selected.head_commit_id) || null,
          keys: [crypto.randomUUID(), crypto.randomUUID(), crypto.randomUUID()] };
        setFrozen(true);
      }
      const saved = attempt.current;
      if (!saved.configId) {
        if (saved.expectedConfigVersion === undefined) {
          const repository = await call(window.agentgit.getRepository());
          if (typeof repository.config_version !== 'number' || !Number.isSafeInteger(repository.config_version) || repository.config_version < 0) throw new Error('后端缺少有效配置版本，请更新后端并核对项目');
          saved.expectedConfigVersion = repository.config_version;
        }
        const value = await call(window.agentgit.createAgent({ config: saved.config, expectedVersion: saved.expectedConfigVersion, key: saved.keys[0]! })); saved.configId = str(value.config_id); saved.configVersion = Number(value.version);
      }
      if (!saved.taskId) { const value = await call(window.agentgit.createTask({ configId: saved.configId, configVersion: saved.configVersion!, goal: saved.goal, key: saved.keys[1]! })); saved.taskId = str(value.task_id); }
      const accepted = await call(window.agentgit.submitRun({ taskId: saved.taskId, configVersion: saved.configVersion!, branchName: saved.branch,
        expectedBranchVersion: saved.branchVersion, expectedHead: saved.head, key: saved.keys[2]! }));
      onAccepted(str(accepted.run_id));
    }); }}><label>研究目标<textarea rows={3} maxLength={4000} value={goal} disabled={frozen} onChange={event => setGoal(event.target.value)} /></label>
      <div className="inline-form"><label className="grow">模型<select aria-label="模型" value={model} disabled={frozen || !choices.length} onChange={event => { setModel(event.target.value); setMoney(''); action.clearError(); }}>{choices.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label><label className="grow">目标分支<select value={branch} disabled={frozen} onChange={event => setBranch(event.target.value)}>{rows(branches.data?.items).map(item => <option key={str(item.name)}>{str(item.name)}</option>)}</select></label><label className="check"><input type="checkbox" checked={execution} disabled={frozen} onChange={event => setExecution(event.target.checked)} />启用固定五工具</label></div>
      {choice?.external ? <div className="model-options"><p>任务上下文将发送到所选供应商。账户访问未验证；凭据需由独立 Worker 预置，此处不收集密钥。</p><label>费用上限（USD）<input value={money} disabled={frozen} inputMode="decimal" maxLength={32} onChange={event => { setMoney(event.target.value); action.clearError(); }} placeholder="例如 1.00" aria-invalid={Boolean(moneyError)} aria-describedby="model-money-hint" required /></label><p id="model-money-hint" className="small muted">{moneyError || `最多 6 位小数。按当前配置，每次调用先预留 ${usd(choice.minimum.cost_micro_usd)}；预算按价格表估计，不是供应商账单。`}</p></div> : <p className="small muted">Mock 离线运行，不调用外部模型，也不需要供应商密钥。</p>}
      <details><summary>完整配置与预算</summary><p className="muted small">以下为配置模板；提交前合并预算。仅支持后端公开的模型与工具，所有预算值须为安全整数。{choice?.external ? '费用上限使用上方 USD 输入；JSON 中同名字段须一致。' : ''}</p><JsonView value={{ ...config, ...choice?.patch }} /><label>预算 JSON（留空使用上方默认 limits）<textarea className="mono" rows={5} value={limits} disabled={frozen} onChange={event => setLimits(event.target.value)} placeholder={JSON.stringify(config.limits, null, 2)} spellCheck={false} /></label></details>
      <div className="form-end"><span className="muted small">{frozen ? `保留原动作键 · Task ${attempt.current?.taskId ?? '尚未受理'}` : '当前分支 HEAD/version 将作为提交前置条件'}</span><button className="primary" disabled={action.busy || !goal.trim() || !choice || !branches.data || Boolean(choice.external && !money.trim())}>{action.busy ? '提交中…' : frozen ? '使用原幂等键继续' : choice?.external ? '提交外部模型运行' : '提交运行'}</button></div><Notice>{action.error}</Notice>
    </form></section>;
}
