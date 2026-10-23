import React, { useRef, useState } from 'react';
import type { Inputs, JsonObject } from '../shared/bridge';
import { DIGEST, UUID, digest, draft, experimentRequest, hasFeature, identifier, knownRejection, type ExperimentScope, type GroupDraft, type GroupKind } from '../shared/experiments';
import { ExperimentStatistics } from './ExperimentStatistics';
import { ExperimentTrials } from './ExperimentTrials';
import { InterventionContext } from './InterventionContext';
import { call, date, Empty, JsonView, Notice, object, rows, short, str, useAction, useRead, type PageProps } from './ui';

function GroupFields({ name, value, change, disabled }: { name: string; value: GroupDraft; change: (value: GroupDraft) => void; disabled: boolean }) {
  return <fieldset disabled={disabled}><legend>{name} 组条件</legend><label>{name} 组名称<input maxLength={200} value={value.label} onChange={event => change({ ...value, label: event.target.value })} /></label>
    <label>{name} 组条件类型<select value={value.kind} onChange={event => change({ ...value, kind: event.target.value as GroupKind })}><option value="inherit">沿用来源条件</option><option value="prompt">完整 Prompt</option><option value="plan">完整计划 JSON</option><option value="interventions">有序干预 ID</option></select></label>
    {value.kind === 'prompt' && <label>{name} 组完整 Prompt<textarea rows={7} maxLength={16000} value={value.prompt} onChange={event => change({ ...value, prompt: event.target.value })} /></label>}
    {value.kind === 'plan' && <label>{name} 组完整计划 JSON<textarea rows={7} className="mono" spellCheck={false} value={value.plan} onChange={event => change({ ...value, plan: event.target.value })} /></label>}
    {value.kind === 'interventions' && <label>{name} 组干预 ID<textarea rows={5} className="mono" spellCheck={false} value={value.ids} onChange={event => change({ ...value, ids: event.target.value })} /><span className="small muted">1–8 个既有 UUID，按填写顺序执行；用逗号或换行分隔。</span></label>}
  </fieldset>;
}

export default function Experiments(props: PageProps & { active: boolean; capabilities: JsonObject | null }) {
  const [source, setSource] = useState(props.runId), [from, setFrom] = useState(props.selectedCommit), [split, setSplit] = useState<ExperimentScope['fixtureSplit']>('screen');
  const [scope, setScope] = useState<ExperimentScope | null>(null), [validatorRef, setValidatorRef] = useState(''), [freezeSettled, setFreezeSettled] = useState(false);
  const freezeAttempt = useRef<Inputs['freezeValidator'] | null>(null), freeze = useAction();
  const [groups, setGroups] = useState<[GroupDraft, GroupDraft]>([draft('来源条件'), draft('对照条件')]);
  const [repeats, setRepeats] = useState('5'), [budget, setBudget] = useState(''), [money, setMoney] = useState('');
  const [mode, setMode] = useState<'live' | 'hybrid'>('live'), [until, setUntil] = useState(''), [calls, setCalls] = useState(''), [miss, setMiss] = useState<'stop' | 'fixture'>('stop');
  const submission = useRef<Inputs['createExperiment'] | null>(null), submit = useAction();
  const [locked, setLocked] = useState(false), [submitSettled, setSubmitSettled] = useState(false), [acceptedId, setAcceptedId] = useState(''), [patchPending, setPatchPending] = useState(false);
  const [lookup, setLookup] = useState(''), [experimentId, setExperimentId] = useState(''), lookupAction = useAction(), [afterId, setAfterId] = useState('');
  const lastReport = useRef<JsonObject>({});
  const selectedReport = useRef('');
  const runs = useRead(() => props.active ? call(window.agentgit.listRuns({ limit: 100 })) : Promise.resolve({} as JsonObject), [props.active]);
  const sourceId = source.trim().toLowerCase();
  const run = useRead(() => props.active && UUID.test(sourceId) ? call(window.agentgit.getRun({ runId: sourceId })) : Promise.resolve({} as JsonObject), [sourceId, props.active]);
  const sourceReady = UUID.test(sourceId) && str(run.data?.id).toLowerCase() === sourceId;
  const branch = sourceReady ? str(run.data?.branch_name) : '';
  const commits = useRead(() => props.active && branch ? call(window.agentgit.listCommits({ branchName: branch, limit: 100 })) : Promise.resolve({} as JsonObject), [branch, props.active]);
  const validator = useRead(() => props.active && validatorRef ? call(window.agentgit.getValidator({ validatorRef })) : Promise.resolve({} as JsonObject), [validatorRef, props.active], 0, validatorRef);
  const validatorReady = validator.data?.validator_ref === validatorRef && object(validator.data?.manifest).kind === 'frozen_research_validator/v1';
  const listing = useRead(() => props.active ? call(window.agentgit.listExperiments({ limit: 20, ...(afterId ? { afterId } : {}) })) : Promise.resolve({} as JsonObject), [afterId, props.active]);
  const report = useRead(async () => {
    if (!props.active || !experimentId) return lastReport.current;
    const value = await call(window.agentgit.getExperiment({ experimentId }));
    if (value.experiment_id !== experimentId) throw new Error('实验报告身份无法核对');
    if (selectedReport.current === experimentId) lastReport.current = value;
    return value;
  }, [experimentId, props.active], props.active && experimentId ? 3000 : 0, experimentId);
  const currentReport = report.data?.experiment_id === experimentId ? report.data : null;
  const external = run.data?.model_external === true || str(run.data?.model).startsWith('openai/');
  function open(id: string) { if (selectedReport.current !== id) lastReport.current = {}; else report.refresh(); selectedReport.current = id; setExperimentId(id); setLookup(id); }
  function updateGroup(index: 0 | 1, value: GroupDraft) { setGroups(previous => index === 0 ? [value, previous[1]] : [previous[0], value]); }
  function resetSource() {
    freezeAttempt.current = null; setScope(null); setValidatorRef(''); setFreezeSettled(false); freeze.clearError();
    submission.current = null; setLocked(false); setSubmitSettled(false); setAcceptedId(''); submit.clearError(); setCalls(''); setUntil('');
    setGroups([draft('来源条件'), draft('对照条件')]); setBudget(''); setMoney(''); setMode('live'); setRepeats('5');
  }
  function freezeValidator() { void freeze.run(async () => {
    if (!freezeAttempt.current) {
      if (!sourceReady) throw new Error('请等待来源 Run 核对完成');
      const next = { sourceRunId: identifier(source, '来源 Run'), fromCommit: digest(from, '安全起点'), fixtureSplit: split };
      freezeAttempt.current = { ...next, key: crypto.randomUUID() }; setScope(next);
    }
    const reply = await window.agentgit.freezeValidator(freezeAttempt.current);
    if (!reply.ok) { setFreezeSettled(knownRejection(reply.error.code)); throw new Error(`${reply.error.message}（${reply.error.code}）`); }
    setValidatorRef(digest(str(reply.data.validator_ref), '冻结验收器')); setFreezeSettled(true);
  }); }
  function createExperiment() { void submit.run(async () => {
    if (!submission.current) {
      if (!scope || !validatorReady || !sourceReady || sourceId !== scope.sourceRunId || patchPending) throw new Error('请先完成来源、冻结验收器与在途干预核对');
      const request = experimentRequest({ scope, validatorRef, repeats, run: run.data, budget, money, groups, mode, until, calls, miss });
      submission.current = { request, key: crypto.randomUUID() }; setLocked(true);
    }
    const reply = await window.agentgit.createExperiment(submission.current);
    if (!reply.ok) { setSubmitSettled(knownRejection(reply.error.code)); throw new Error(`${reply.error.message}（${reply.error.code}）`); }
    const id = identifier(str(reply.data.experiment_id), '已提交实验'); setSubmitSettled(true); setAcceptedId(id); open(id); listing.refresh();
  }); }
  return <div className="experiments">
    {!props.active && <Notice>受控实验能力尚未确认；已固定的请求保留，恢复连接后可继续查询。</Notice>}
    <div className="experiment-layout"><section className="panel"><h2>建立受控 A/B 实验</h2><p className="muted">共同起点、验收规则、模型和预算；每个 trial 使用独立 Run 与工作区。</p>
      <fieldset disabled={scope !== null || !props.active}><legend>共同来源</legend><label>实验来源 Run<input list="experiment-runs" value={source} placeholder="来源 Run UUID" spellCheck={false} onChange={event => { setSource(event.target.value); setFrom(''); }} /></label>
        <datalist id="experiment-runs">{rows(runs.data?.items).map(item => <option key={str(item.id)} value={str(item.id)}>{str(object(item.goal).text) || str(item.branch_name)}</option>)}</datalist>
        <label>实验安全起点 Commit<input list="experiment-commits" value={from} placeholder="完整 64 位摘要" spellCheck={false} onChange={event => setFrom(event.target.value)} /></label>
        <datalist id="experiment-commits">{rows(commits.data?.items).map(item => <option key={str(item.commit_id)} value={str(item.commit_id)} />)}</datalist>
        <label>固定语料 split<select value={split} onChange={event => setSplit(event.target.value as typeof split)}><option value="screen">screen · 筛选</option><option value="confirm">confirm · 确认</option><option value="regression">regression · 回归</option></select></label>
      </fieldset>
      <p className="small muted">候选列表最多 100 项，也可填写完整标识。起点归属与安全性由后端重新核验。</p>
      <Notice>{run.error || commits.error || freeze.error}</Notice>
      <div className="button-group"><button disabled={!props.active || freeze.busy || Boolean(validatorRef) || (!scope && (!sourceReady || !DIGEST.test(from.trim())))} onClick={freezeValidator}>{scope && !freezeSettled ? '用原键继续冻结' : '冻结共同验收器'}</button>
        {scope && <button disabled={freeze.busy || submit.busy || !freezeSettled || (locked && !submitSettled) || patchPending} onClick={resetSource}>重新选择来源</button>}</div>
      <form onSubmit={event => { event.preventDefault(); createExperiment(); }}><fieldset disabled={locked || !validatorReady || !props.active}><legend>试验条件</legend>
        <div className="experiment-groups">{groups.map((group, index) => <GroupFields key={index} name={index === 0 ? 'A' : 'B'} value={group} disabled={locked || !validatorReady} change={value => updateGroup(index as 0 | 1, value)} />)}</div>
        {groups.every(group => group.kind === 'inherit') && <p className="small muted">两组均沿用来源：只比较同条件重复结果，不说明干预提升。</p>}
        <label>每组重复次数<input type="number" min={1} max={10} step={1} value={repeats} onChange={event => setRepeats(event.target.value)} /></label><p className="small muted">默认各 5 次，共 10 个独立 trial。共同预算对每个 trial 分别生效。</p>
        <label>实验执行模式<select value={mode} onChange={event => setMode(event.target.value as typeof mode)}><option value="live">Live · 独立重执行</option><option value="hybrid" disabled={!hasFeature(props.capabilities, 'hybrid_observations')}>Hybrid · 选定只读观测复用</option></select></label>
        {mode === 'hybrid' && <><label>Hybrid 录制范围终点<input list="experiment-commits" value={until} onChange={event => setUntil(event.target.value)} spellCheck={false} /></label><label>Hybrid 选定只读调用 ID<textarea rows={4} className="mono" value={calls} onChange={event => setCalls(event.target.value)} spellCheck={false} /></label><label>观测未精确匹配时<select value={miss} onChange={event => setMiss(event.target.value as typeof miss)}><option value="stop">stop · 停止</option><option value="fixture">fixture · 固定模拟器（test_only）</option></select></label><p className="small muted">只接受同参数、同签名的受控只读录制；模拟或合成观测会标为 test_only。</p></>}
        {external && <label>每个 trial 费用上限（USD）<input inputMode="decimal" value={money} onChange={event => setMoney(event.target.value)} placeholder="显式填写，例如 0.50" /><span className="small muted">每个 trial 可产生外部调用，合计最多为此上限 × 两组次数；价格表估计不等于账单。</span></label>}
        <details><summary>高级：共同预算 JSON</summary><label>每个 trial 的完整 BudgetLimits<textarea rows={5} className="mono" value={budget} onChange={event => setBudget(event.target.value)} placeholder="留空复制来源预算；后端核对不扩大来源授权" spellCheck={false} /></label></details>
      </fieldset><Notice>{submit.error}</Notice><div className="form-end"><span className="small muted">{locked ? '请求与幂等键已固定' : '提交会原子排队两组全部试验'}</span><button className="primary" type="submit" disabled={!props.active || submit.busy || !validatorReady || patchPending || Boolean(acceptedId) || (locked && submitSettled)}>{locked && !submitSettled ? '用原键继续提交' : '提交 A/B 实验'}</button></div></form>
      {locked && submitSettled && <button onClick={() => { submission.current = null; setLocked(false); setSubmitSettled(false); setAcceptedId(''); submit.clearError(); }}>开始另一实验</button>}
      {submission.current && <details><summary>本次固定请求与幂等键</summary><JsonView value={submission.current} /></details>}
    </section><aside className="branch-aside"><section className="explanation"><h2>共同验收边界</h2><p>验收器固定语料、时间、规则和摘要。切换来源需重新冻结；A/B 不能更改模型、权限或验收规则。</p><Notice>{validator.error}</Notice>
      {sourceReady && <><dl className="facts"><div><dt>来源模型</dt><dd>{str(run.data?.model) || '未知'}</dd></div><div><dt>来源状态</dt><dd>{str(run.data?.status) || '未知'}</dd></div></dl><details><summary>来源预算与运行摘要</summary><JsonView value={run.data ?? undefined} /></details></>}
      {validatorReady ? <><p>已冻结 <code>{short(validatorRef)}</code> · {scope?.fixtureSplit}</p><details><summary>查看验收规则与固定输入</summary><JsonView value={validator.data} /></details></> : <p className="muted">尚未取得可兼容的冻结验收器。旧代码或不兼容规则不能据此重新判分。</p>}
    </section>{scope && validatorReady && hasFeature(props.capabilities, 'typed_interventions') && <InterventionContext key={`${scope.sourceRunId}:${scope.fromCommit}:${validatorRef}`} scope={scope} validatorRef={validatorRef} active={props.active} disabled={locked} onPending={setPatchPending} onPrompt={prompt => updateGroup(1, { ...groups[1], kind: 'prompt', prompt })} onCall={id => setCalls(previous => previous.includes(id) ? previous : `${previous.trim()}\n${id}`.trim())} onIntervention={id => {
      if (!['inherit', 'interventions'].includes(groups[1].kind)) throw new Error('B 组已有直接 Prompt 或计划，请先切换为沿用来源或干预 ID');
      const ids = groups[1].ids.trim().split(/[\s,]+/).filter(Boolean); if (!ids.includes(id)) ids.push(id); if (ids.length > 8) throw new Error('B 组最多 8 个干预'); updateGroup(1, { ...groups[1], kind: 'interventions', ids: ids.join('\n') });
    }} />}</aside></div>
    <section className="panel experiment-history"><h2>查询已保存实验</h2><form className="inline-form" onSubmit={event => { event.preventDefault(); void lookupAction.run(async () => open(identifier(lookup, '实验 ID'))); }}><label className="grow">Experiment UUID<input value={lookup} spellCheck={false} onChange={event => setLookup(event.target.value)} /></label><button disabled={!props.active} type="submit">只读查询实验</button></form><Notice>{lookupAction.error || listing.error}</Notice>
      {!rows(listing.data?.items).length ? <p className="muted">本页尚无已保存实验。</p> : <ul className="experiment-list">{rows(listing.data?.items).map(item => <li key={str(item.id)}><button onClick={() => open(str(item.id))}>查看实验 {short(item.id)}</button><span className="small muted">{date(item.created_at)} · {String(item.trial_count)} trial · 来源 {short(item.source_run_id)} · 起点 {short(item.from_commit_id)}</span></li>)}</ul>}
      <div className="button-group"><button disabled={!afterId || !props.active} onClick={() => setAfterId('')}>回到第一页</button><button disabled={!listing.data?.has_more || !props.active} onClick={() => setAfterId(str(listing.data?.next_after_id))}>下一页实验</button><button disabled={!props.active} onClick={listing.refresh}>刷新实验列表</button></div>
    </section>
    {experimentId && <section className="panel experiment-report"><h2>实验报告 <code>{short(experimentId)}</code></h2><Notice>{report.error && `${currentReport ? '显示上次已确认结果；刷新失败：' : ''}${report.error}`}</Notice>
      {currentReport ? <><p>模式 {str(object(currentReport.manifest).mode)} · {currentReport.test_only === true || object(currentReport.manifest).test_only === true ? 'test_only · 试验结果' : '按冻结规则验收'}</p><details><summary>实验 manifest 与共同输入</summary><JsonView value={currentReport.manifest} /></details><ExperimentStatistics statistics={object(currentReport.statistics)} /><ExperimentTrials key={experimentId} trials={rows(currentReport.trials)} refresh={report.refresh} openTimeline={props.openTimeline} /></> : <Empty title={report.loading ? '正在读取实验…' : '暂无兼容报告'}>只读查询不会提交或重新执行任务。请核对项目、实验 ID 与验收器兼容性。</Empty>}
    </section>}
  </div>;
}
