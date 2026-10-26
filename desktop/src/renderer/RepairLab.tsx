import React, { useEffect, useRef, useState } from 'react';
import type { JsonObject, Reply } from '../shared/bridge';
import { count, DIGEST, hasFeature, identifier, knownRejection, money, UUID } from '../shared/experiments';
import { preparedRepair, repairReviewGate, type RegressionDraft } from '../shared/repair';
import { call, Empty, JsonView, Notice, object, rows, short, Status, str, useAction, useRead, type PageProps } from './ui';

type Bundle = { links: JsonObject; search: JsonObject; study: JsonObject; validation: JsonObject; proposal: JsonObject };
type Attempt = { label: string; send: () => Promise<Reply<JsonObject>>; accepted?: (value: JsonObject) => void; refresh?: boolean };
const EMPTY: Bundle = { links: {}, search: {}, study: {}, validation: {}, proposal: {} };

function FailureAssertions({ manifest, open }: { manifest: JsonObject; open: (id: string) => void }) {
  const assertions = manifest.failure_assertions;
  return <section><h3>冻结失败断言</h3>{Array.isArray(assertions) && assertions.length ? <ul>{assertions.map((item, index) => {
    const assertion = object(item), event = object(assertion.event), runId = str(event.run_id);
    return <li key={index}><code>{typeof item === 'string' ? item : str(assertion.code) || '未命名断言'}</code>{UUID.test(runId) && <button onClick={() => open(runId)}>查看来源运行</button>}<details><summary>断言原文与事件引用</summary><JsonView value={item} /></details></li>;
  })}</ul> : <p>暂无可展示断言，请核对冻结输入。</p>}</section>;
}

function Usage({ value }: { value: JsonObject }) {
  return <><dl className="facts"><div><dt>实际模型调用</dt><dd>{count(value.model_calls)}</dd></div><div><dt>实际工具调用</dt><dd>{count(value.tool_calls)}</dd></div><div><dt>已计量单位</dt><dd>{count(value.tokens)}</dd></div><div><dt>已知费用</dt><dd>{money(value.cost_micro_usd)}</dd></div><div><dt>未结算试验</dt><dd>{count(value.active)}</dd></div><div><dt>墙钟毫秒</dt><dd>{count(value.elapsed_wall_ms)}</dd></div></dl><details><summary>额度、阶段次数与完整用量</summary><JsonView value={value} /></details><p className="small muted">Mock 按 UTF-8 字节计量；供应商计量与估计费用以收据为准。缺失值不当作零。</p></>;
}

function Trials({ title, trials, open }: { title: string; trials: JsonObject[]; open: (id: string) => void }) {
  if (!trials.length) return null;
  return <section><h3>{title} · {trials.length} 项</h3><div className="table-scroll"><table><thead><tr><th>阶段 / 试验</th><th>结果</th><th>本次调用 / 费用</th><th>执行与证据</th></tr></thead><tbody>{trials.map((trial, index) => {
    const result = object(trial.result), dispatch = object(trial.dispatch), usage = object(result.usage), run = str(result.current_run_id ?? dispatch.initial_run_id);
    return <tr key={str(trial.trial_id) || index}><td>{str(result.phase ?? trial.operation) || title}<br /><code>{short(trial.trial_id)}</code></td><td><Status value={result.verdict ?? trial.status} /><br />{str(result.category)}{result.test_only === true && <span> · test_only</span>}</td><td>{count(usage.model_calls)} / {money(usage.cost_micro_usd)}</td><td>{UUID.test(run) && <button onClick={() => open(run)}>查看执行</button>}<details><summary>试验收据与投影</summary><JsonView value={trial} /></details></td></tr>;
  })}</tbody></table></div></section>;
}

export default function RepairLab(props: PageProps & { active: boolean }) {
  const [source, setSource] = useState(props.runId), [base, setBase] = useState(props.selectedCommit), [validator, setValidator] = useState('');
  const [targets, setTargets] = useState('[]'), [budget, setBudget] = useState(''), [hybrid, setHybrid] = useState(''), [stochastic, setStochastic] = useState(false), [consent, setConsent] = useState(false);
  const [regression, setRegression] = useState<RegressionDraft[]>(() => Array.from({ length: 4 }, () => ({ run: '', commit: '' })));
  const [lookup, setLookup] = useState(''), [selected, setSelected] = useState(''), [afterId, setAfterId] = useState(''), [created, setCreated] = useState(false);
  const [ticket, setTicket] = useState<JsonObject | null>(null), [attempt, setAttempt] = useState<Attempt | null>(null), [waiting, setWaiting] = useState(false);
  const pending = useRef<Attempt | null>(null), action = useAction();
  const lastBundle = useRef<Bundle>(EMPTY), currentId = useRef('');
  const caps = useRead(() => props.active ? call(window.agentgit.capabilities()) : Promise.resolve({} as JsonObject), [props.active], 0, 'capabilities');
  const supported = hasFeature(caps.data, 'prepared_repair_search') && hasFeature(caps.data, 'repair_workflow');
  const sourceId = source.trim().toLowerCase();
  const run = useRead(() => props.active && UUID.test(sourceId) ? call(window.agentgit.getRun({ runId: sourceId })) : Promise.resolve({} as JsonObject), [sourceId, props.active]);
  const sourceReady = run.data?.id === sourceId && ['failed', 'budget_exhausted'].includes(str(run.data.status));
  const external = run.data?.model_external === true || str(run.data?.model).startsWith('openai/');
  const fixed = useRead(() => props.active && DIGEST.test(validator) ? call(window.agentgit.getValidator({ validatorRef: validator })) : Promise.resolve({} as JsonObject), [validator, props.active]);
  const fixedReady = fixed.data?.validator_ref === validator && object(fixed.data.manifest).fixture_split === 'screen';
  const listing = useRead(() => props.active && supported ? call(window.agentgit.listRepairSearches({ limit: 20, ...(afterId ? { afterId } : {}) })) : Promise.resolve({} as JsonObject), [afterId, supported, props.active], 0, 'search-list');
  const report = useRead(async (): Promise<Bundle> => {
    if (!selected) return EMPTY;
    if (!props.active) return lastBundle.current;
    const links = await call(window.agentgit.getRepairWorkflow({ searchId: selected }));
    if (links.search_id !== selected) throw new Error('修复流程身份不匹配');
    const studyId = str(links.study_id), proposalId = str(links.proposal_id);
    const [search, study, validation, proposal] = await Promise.all([
      call(window.agentgit.getRepairSearch({ searchId: selected })),
      studyId ? call(window.agentgit.getRepairStudy({ studyId })) : Promise.resolve({} as JsonObject),
      studyId ? call(window.agentgit.getRepairValidation({ studyId })) : Promise.resolve({} as JsonObject),
      proposalId ? call(window.agentgit.getRepairProposal({ proposalId })) : Promise.resolve({} as JsonObject),
    ]);
    if (search.search_id !== selected || (studyId && study.study_id !== studyId) || (proposalId && proposal.proposal_id !== proposalId)) throw new Error('修复记录身份无法核对');
    const value = { links, search, study, validation, proposal };
    if (currentId.current === selected) lastBundle.current = value;
    return value;
  }, [selected, props.active], props.active && selected ? 3000 : 0, selected);
  useEffect(() => { if (report.data?.search.search_id === selected) setWaiting(false); }, [report.data, selected]);
  const view = report.data?.search.search_id === selected ? report.data : EMPTY;
  const studyId = str(view.links.study_id), suiteId = str(view.links.suite_id), proposalId = str(view.links.proposal_id);
  const execution = object(view.validation.execution), verification = object(execution.verification ?? execution.provisional_verification);
  const proposal = object(view.proposal.proposal), gate = repairReviewGate(view.proposal, ticket);
  const blocked = action.busy || attempt !== null || waiting || !props.active;
  function open(id: string) { const value = identifier(id, '搜索'); if (value !== selected) { currentId.current = value; lastBundle.current = EMPTY; setSelected(value); setTicket(null); } else report.refresh(); setLookup(value); }
  async function execute(next: Attempt) {
    const reply = await next.send();
    if (!reply.ok) {
      if (knownRejection(reply.error.code)) { pending.current = null; setAttempt(null); }
      if (reply.error.code === 'version_conflict') { setTicket(null); report.refresh(); }
      throw new Error(`${reply.error.message}（${reply.error.code}）`);
    }
    pending.current = null; setAttempt(null);
    if (next.refresh !== false) setWaiting(true);
    next.accepted?.(reply.data); report.refresh(); listing.refresh();
  }
  function begin(build: () => Attempt) { void action.run(async () => { if (pending.current) throw new Error('请先确认原请求结果'); const next = build(); pending.current = next; setAttempt(next); await execute(next); }); }
  function prepare() { begin(() => {
    const request = preparedRepair({ run: source, base, validator, targets, regression, budget, hybrid, external, stochastic }), key = crypto.randomUUID();
    return { label: '搜索与预注册', send: () => window.agentgit.createPreparedRepairSearch({ request, key }), accepted: value => { setCreated(true); setConsent(false); open(str(value.search_id)); } };
  }); }
  function decision(kind: 'approve' | 'reject' | 'save_experiment') { begin(() => {
    if (!(kind === 'approve' ? gate.approve : kind === 'reject' ? gate.reject : gate.save) || !ticket) throw new Error('请重新核对精确建议及审阅票据');
    const input = { proposalId, reviewId: str(ticket.review_id), proposalRef: str(view.proposal.proposal_ref), expectedConfigVersion: Number(object(ticket.ticket).expected_config_version), decision: kind, key: crypto.randomUUID() };
    return { label: kind === 'approve' ? '创建配置版本' : kind === 'reject' ? '拒绝建议' : '保存实验定义', send: () => window.agentgit.decideRepairReview(input), accepted: () => setTicket(null) };
  }); }
  return <div className="experiments repair-lab"><div className="page-heading"><div><h1>修复实验室</h1><p>从失败证据到精确改动，分别核对筛选、缩减、独立复测与审阅。</p></div><button onClick={() => { setCreated(false); setConsent(false); }} disabled={blocked}>新建另一项搜索</button></div>
    <Notice>{caps.error || (!supported && props.active && caps.data ? '当前后端缺少原子准备或只读流程能力，请更新后端后重新连接。' : '')}</Notice>
    <Notice>{action.error}</Notice>{attempt && <div className="notice"><p>{attempt.label}结果尚未确认。参数与原键已冻结；请确认原请求，勿重新定义动作。</p><button disabled={action.busy || !props.active} onClick={() => void action.run(() => execute(pending.current!))}>用原键确认请求</button></div>}
    {waiting && <p role="status">已收到提交结果，正在只读核对流程。<button onClick={report.refresh}>重新查询</button></p>}
    <details className="panel" open={!selected}><summary>准备失败来源与固定回归</summary><p>当前使用完整有限模板。S8 保留集定位未达目标，候选排序不证明根因。默认最多 8 原子、组合深度 2；30 次额度按 14/6/6/4 分配，完整额度由后端冻结。</p>
      <fieldset disabled={blocked || created || !supported}><legend>共同输入</legend><div className="repair-pair"><label>失败 Run UUID<input value={source} spellCheck={false} onChange={event => { setSource(event.target.value); setValidator(''); }} /></label><label>模型决策前起点<input value={base} spellCheck={false} onChange={event => { setBase(event.target.value); setValidator(''); }} /></label></div><Notice>{run.error}</Notice><p>来源：{sourceReady ? '已核对失败终态' : '请核对失败 Run 及可恢复起点'} · {external ? '外部模型，须显式预算' : '模型条件以来源为准'}</p>
        <div className="inline-form"><label className="grow">screen 验收器摘要<input value={validator} spellCheck={false} onChange={event => setValidator(event.target.value)} /></label><button disabled={!sourceReady || !DIGEST.test(base)} onClick={() => begin(() => { const input = { sourceRunId: sourceId, fromCommit: base, fixtureSplit: 'screen' as const, key: crypto.randomUUID() }; return { label: '冻结验收器', refresh: false, send: () => window.agentgit.freezeValidator(input), accepted: value => setValidator(str(value.validator_ref)) }; })}>冻结来源验收器</button></div><Notice>{fixed.error}</Notice>{fixedReady && <details><summary>已冻结的验收范围</summary><JsonView value={fixed.data?.manifest} /></details>}
        <label>有限事件目标 JSON<textarea rows={4} spellCheck={false} className="mono" value={targets} onChange={event => setTargets(event.target.value)} /><span className="small muted">1–8 个 event_id。观测目标可另含 replacement_call_ids；引用完整同参录制，并配置 Hybrid 范围。</span></label>
        <fieldset><legend>预注册四个不同回归任务</legend>{regression.map((row, index) => <div className="repair-pair" key={index}><label>回归 {index + 1} Run UUID<input value={row.run} spellCheck={false} onChange={event => setRegression(previous => previous.map((value, i) => i === index ? { ...value, run: event.target.value } : value))} /></label><label>回归 {index + 1} 模型决策前起点<input value={row.commit} spellCheck={false} onChange={event => setRegression(previous => previous.map((value, i) => i === index ? { ...value, commit: event.target.value } : value))} /></label></div>)}</fieldset>
        <details><summary>完整预算与 Hybrid 条件</summary><label>完整修复预算 JSON（Mock 可沿用默认）<textarea rows={5} value={budget} spellCheck={false} className="mono" onChange={event => setBudget(event.target.value)} /></label><label>Hybrid 范围 JSON（可选）<textarea rows={4} value={hybrid} spellCheck={false} className="mono" onChange={event => setHybrid(event.target.value)} /></label><label><input type="checkbox" checked={stochastic} disabled={external} onChange={event => setStochastic(event.target.checked)} />来源 Mock 具有预先声明的随机行为，仅形成经验缩减</label></details>
        <label><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} />我已核对来源、四项回归和预算，同意提交后由 Worker 在额度内执行</label><button className="primary" disabled={!sourceReady || !fixedReady || !consent || regression.some(row => !UUID.test(row.run.trim()) || !DIGEST.test(row.commit))} onClick={prepare}>{created ? '搜索已提交' : '准备并搜索最小修复'}</button>
      </fieldset>
    </details>
    <section className="panel experiment-history"><h2>既有搜索</h2><div className="inline-form"><label className="grow">搜索 UUID<input value={lookup} onChange={event => setLookup(event.target.value)} spellCheck={false} /></label><button disabled={!UUID.test(lookup.trim()) || !props.active} onClick={() => open(lookup)}>打开搜索</button><button disabled={!props.active} onClick={() => { listing.refresh(); report.refresh(); }}>只读刷新</button></div><Notice>{listing.error}</Notice><ul className="experiment-list">{rows(listing.data?.items).map(item => <li key={str(item.search_id)}><button onClick={() => open(str(item.search_id))} disabled={attempt !== null}>{short(item.search_id)}</button><Status value={item.status} /><span>来源 {short(item.failed_run_id)}</span></li>)}</ul><div className="button-group"><button disabled={!afterId} onClick={() => setAfterId('')}>第一页搜索</button><button disabled={!listing.data?.has_more} onClick={() => setAfterId(str(listing.data?.next_after_id))}>下一页搜索</button></div></section>
    <Notice>{report.error && `${view !== EMPTY ? '显示上次已确认结果；刷新失败：' : ''}${report.error}`}</Notice>
    {selected && view === EMPTY && <Empty title={report.loading ? '正在读取修复流程…' : '尚无兼容流程'}>查询不会重提搜索、重开样本或执行任何动作。</Empty>}
    {view !== EMPTY && <section className="panel experiment-report"><div className="section-heading"><h2>修复流程 <code>{short(selected)}</code></h2><Status value={execution.status ?? view.study.status ?? view.search.status} /></div><p>搜索 ID <code>{selected}</code></p><p>停止原因：{str(execution.stop_reason ?? view.study.stop_reason ?? view.search.stop_reason) || '尚无停止结论'}</p><FailureAssertions manifest={object(view.search.manifest)} open={props.openTimeline} /><Usage value={object(execution.usage ?? view.study.usage ?? view.search.usage)} /><details><summary>冻结输入、失败断言和完整候选</summary><JsonView value={view.search.manifest} /><JsonView value={view.search.proposals} /></details>
      <div className="button-group"><button disabled={blocked || studyId !== '' || view.search.status !== 'needs_confirmation' || !suiteId} onClick={() => begin(() => { const input = { request: { schema_version: 1, search_id: selected }, key: crypto.randomUUID() }; return { label: '开始缩减', send: () => window.agentgit.createRepairStudy(input) }; })}>开始缩减</button><button disabled={blocked || Boolean(studyId) || !['queued', 'running', 'waiting_reconciliation'].includes(str(view.search.status))} onClick={() => begin(() => { const input = { searchId: selected, key: crypto.randomUUID() }; return { label: '取消搜索', send: () => window.agentgit.cancelRepairSearch(input) }; })}>取消搜索</button></div>
      {studyId && <><h3>缩减与候选锁</h3><Status value={view.study.status} /><p>原子范围：{str(object(view.study.reduction).minimality) || '尚未证明'} · 字段范围：{str(object(object(view.study.field_reduction).summary).field_minimality) || '尚无完整字段结论'}。均不表示全局最优。</p><details><summary>完整缩减、字段变换与最终锁</summary><JsonView value={view.study.reduction} /><JsonView value={view.study.field_reduction} /><JsonView value={view.study.final_lock} /></details><div className="button-group"><button disabled={blocked || !suiteId || view.study.status !== 'awaiting_samples' || view.validation.validation !== null} onClick={() => begin(() => { const input = { studyId, suiteId, key: crypto.randomUUID() }; return { label: '开启原预注册池', send: () => window.agentgit.openRepairValidation(input) }; })}>锁后开启独立复测</button><button disabled={blocked || ['verified_under_fixture', 'empirically_confirmed', 'rejected', 'cancelled', 'needs_review'].includes(str(execution.status ?? view.study.status))} onClick={() => begin(() => { const input = { studyId, key: crypto.randomUUID() }; return { label: '取消研究', send: () => window.agentgit.cancelRepairStudy(input) }; })}>取消研究</button></div></>}
      {Object.keys(verification).length > 0 && <section><h3>独立确认与固定回归</h3><Status value={verification.status} /><p>确认 {count(verification.confirm_passed)} / {count(verification.confirm_determinate)} · 回归 {count(verification.regression_passed)} / {count(verification.regression_planned)}</p><p>95% Wilson 概率区间：{str(object(verification.confirmation_interval).lower) || '未知'} — {str(object(verification.confirmation_interval).upper) || '未知'}</p><p>6 次至少 5 次通过、四回归全通过和安全零违规才可附范围验证。小样本不证明线上稳定；test_only 与来源限制保持。</p><JsonView value={verification} /></section>}
      {studyId && <button disabled={blocked || Boolean(proposalId) || !execution.verification} onClick={() => begin(() => { const input = { studyId, key: crypto.randomUUID() }; return { label: '生成精确建议', send: () => window.agentgit.createRepairProposal(input) }; })}>生成精确修复建议</button>}
      <Trials title="筛选（含恢复预检）" trials={rows(view.search.trials)} open={props.openTimeline} /><Trials title="原子删除" trials={rows(view.study.trials)} open={props.openTimeline} /><Trials title="字段与必要性复查" trials={rows(object(view.study.field_reduction).trials)} open={props.openTimeline} /><Trials title="独立确认 / 回归" trials={rows(execution.trials)} open={props.openTimeline} />
      {proposalId && <section className="repair-proposal"><h2>精确修复建议</h2><p>建议摘要 <code>{str(view.proposal.proposal_ref)}</code></p><p>来源配置 <code>{str(proposal.source_config_id)}</code> · 版本 {count(proposal.source_config_version)} · 当前项目计数器 {count(view.proposal.current_config_version)}</p>
        {gate.observed && <Notice>包含工具观测替换：test_only，仅适用于已声明的实验范围。正式配置采纳不可用，只能保存实验定义。</Notice>}
        <div className="repair-pair"><section><h3>来源配置原文</h3><JsonView value={proposal.before} /></section><section><h3>{gate.observed ? '仅实验补丁，不创建正式配置' : '建议配置原文'}</h3><JsonView value={gate.observed ? proposal.patches : proposal.after} /></section></div><details><summary>适用任务、环境、完整补丁与证据</summary><JsonView value={proposal} /></details>
        {view.proposal.review !== null ? <><h3>已记录的决定</h3><JsonView value={view.proposal.review} /><p>决定不会自动启动任务。新配置仅供后续显式创建任务使用；保存实验只保存定义。</p></> : <><button disabled={blocked} onClick={() => begin(() => { const input = { proposalId, key: crypto.randomUUID() }; return { label: '获取当前审阅票据', refresh: false, send: () => window.agentgit.beginRepairReview(input), accepted: setTicket }; })}>核对并获取审阅票据</button>
          {ticket && <><details open><summary>当前精确审阅票据</summary><JsonView value={ticket} /></details>{!gate.exact && <Notice>配置版本或建议绑定已变化。请只读刷新，再显式获取新的审阅票据。</Notice>}<p>批准仅创建一个配置新版本；拒绝不改配置；保存实验不执行。</p><div className="button-group"><button className="primary" disabled={blocked || !gate.approve} onClick={() => decision('approve')}>批准创建配置版本</button><button disabled={blocked || !gate.save} onClick={() => decision('save_experiment')}>仅保存为实验</button><button disabled={blocked || !gate.reject} onClick={() => decision('reject')}>拒绝修复建议</button></div></>}
          {!gate.qualified && <p className="muted">确认或回归未达到完整门槛，不能批准或保存为合格实验；可获取票据后拒绝。</p>}</>}
      </section>}
    </section>}
  </div>;
}
