import React, { useRef, useState } from 'react';
import type { Inputs, Json, JsonObject } from '../shared/bridge';
import { evidenceTarget, candidatePatch, candidatePreset, type CandidateExperimentPreset } from '../shared/diagnosis';
import { digest, exact, hasFeature, identifier, knownRejection } from '../shared/experiments';
import { ExperimentStatistics } from './ExperimentStatistics';
import { call, Empty, JsonView, Notice, object, rows, short, str, useAction, useRead, type EvidenceTarget } from './ui';

const KINDS: Record<string, string> = { accepted_plan: '已接受计划', accepted_tool_decision: '已接受工具决策', unaccepted_model_output: '接受关系未完整核验的模型输出' };
type OpenEvent = (target: EvidenceTarget) => void;
type OpenExperiment = (preset: CandidateExperimentPreset) => void;
type Submission = Inputs['createDebugReport'];

export function EventLink({ event, openEvent, label }: { event: Json | undefined; openEvent: OpenEvent; label?: string }) {
  const target = evidenceTarget(event);
  return target ? <button className="text-button event-link" onClick={() => openEvent(target)} title={`Run ${target.runId} · seq ${target.seq} · ID ${target.eventId}`}>{label ?? `事件 ${target.seq} · ID ${target.eventId}`}</button>
    : <span className="muted small">{label ?? '事件'}：缺少可跳转的精确引用</span>;
}

export default function DebugReports({ failedRunId, openEvent, openExperiment }: { failedRunId: string; openEvent: OpenEvent; openExperiment: OpenExperiment }) {
  const capabilities = useRead(() => call(window.agentgit.capabilities()), []);
  const supported = hasFeature(capabilities.data, 'static_debug_reports');
  return <section className="debug-reports" aria-labelledby="debug-title"><div className="section-heading"><h2 id="debug-title">故障候选与报告</h2><span className="muted small">静态证据与干预效果分别判断</span></div>
    <Notice>{capabilities.error}</Notice>{capabilities.loading ? <p role="status">核对定位能力…</p> : !supported ? <Empty title="当前后端尚不支持故障报告">请连接具有 static_debug_reports 能力的后端后重试；结构化差异仍可独立使用。</Empty> : <ReportWorkspace failedRunId={failedRunId} openEvent={openEvent} openExperiment={openExperiment} />}
  </section>;
}

function ReportWorkspace({ failedRunId, openEvent, openExperiment }: { failedRunId: string; openEvent: OpenEvent; openExperiment: OpenExperiment }) {
  const [failed, setFailed] = useState(failedRunId), [reference, setReference] = useState('');
  const [reportId, setReportId] = useState(''), [lookup, setLookup] = useState(''), [afterId, setAfterId] = useState<string | undefined>();
  const [pending, setPending] = useState<Submission | null>(null), submission = useRef<Submission | null>(null);
  const action = useAction();
  const listing = useRead(() => call(window.agentgit.listDebugReports({ ...(afterId ? { afterId } : {}), limit: 10 })), [afterId]);
  const report = useRead(() => reportId ? call(window.agentgit.getDebugReport({ reportId })) : Promise.resolve(null), [reportId]);
  function openReport(id: string) { if (id === reportId) report.refresh(); else setReportId(id); setLookup(id); }
  function create() {
    void action.run(async () => {
      if (!submission.current) {
        submission.current = { failedRunId: identifier(failed, '失败 Run'), ...(reference.trim() ? { referenceRunId: identifier(reference, '成功参考 Run') } : {}), key: crypto.randomUUID() };
      }
      const request = submission.current; setPending(request);
      const reply = await window.agentgit.createDebugReport(request);
      if (!reply.ok) { if (knownRejection(reply.error.code)) { submission.current = null; setPending(null); } throw new Error(`${reply.error.message}（${reply.error.code}）`); }
      const receipt = reply.data;
      const identity = identifier(str(receipt.debug_report_id), '返回的报告 ID');
      openReport(identity); submission.current = null; setPending(null); listing.refresh();
    });
  }
  return <><p>沿失败断言列出值得验证的决策。排名和首个分歧均不等于已证实根因；未执行试验时改善率未知。</p>
    <form className="debug-create" onSubmit={event => { event.preventDefault(); create(); }}>
      <label>失败 Run UUID<input value={pending?.failedRunId ?? failed} disabled={pending !== null} onChange={event => setFailed(event.target.value)} spellCheck={false} /></label>
      <label>成功参考 Run UUID（可选）<input value={pending?.referenceRunId ?? reference} disabled={pending !== null} onChange={event => setReference(event.target.value)} spellCheck={false} /></label>
      <button className="primary" disabled={action.busy || !failed.trim()}>{pending ? '继续原报告请求' : '生成静态报告'}</button>
    </form>{pending && <p className="small muted" role="status">结果尚未确认；保留原输入和幂等键，重试只确认同一报告。关闭或重开页面不会自动重发。</p>}<Notice>{action.error}</Notice>
    <div className="debug-history"><form className="inline-form" onSubmit={event => { event.preventDefault(); void action.run(async () => openReport(identifier(lookup, '报告 ID'))); }}>
      <label className="grow">已有 Debug Report UUID<input value={lookup} onChange={event => setLookup(event.target.value)} spellCheck={false} /></label><button disabled={!lookup.trim() || action.busy}>打开报告</button><button type="button" disabled={listing.loading} onClick={listing.refresh}>刷新报告列表</button>
    </form><Notice>{listing.error}</Notice>
      <div className="debug-report-list" aria-label="已冻结报告">{rows(listing.data?.items).map(item => <button key={str(item.debug_report_id)} className={reportId === item.debug_report_id ? 'selected' : ''} onClick={() => openReport(str(item.debug_report_id))}><span>报告 <code>{short(item.debug_report_id)}</code></span><span className="small muted">失败 Run <code>{short(item.failed_run_id)}</code>{item.reference_run_id ? ' · 有成功参考' : ' · 无参考'}</span></button>)}</div>
      {!listing.loading && !rows(listing.data?.items).length && <p className="muted">当前页尚无报告。生成报告需要已终止的失败 Run。</p>}
      <div className="pagination"><button disabled={!afterId} onClick={() => setAfterId(undefined)}>报告首批</button><button disabled={!listing.data?.has_more} onClick={() => setAfterId(str(listing.data?.next_after_id))}>更多报告</button></div>
    </div><Notice>{report.error}</Notice>{reportId && report.loading ? <p role="status">读取冻结报告…</p> : report.data ? <ReportDetail key={reportId} stored={report.data} openEvent={openEvent} openExperiment={openExperiment} /> : null}
  </>;
}

function ReportDetail({ stored, openEvent, openExperiment }: { stored: JsonObject; openEvent: OpenEvent; openExperiment: OpenExperiment }) {
  const report = object(stored.report), diagnosis = object(report.diagnosis), failed = object(report.failed), reference = object(report.reference), comparison = object(report.comparison);
  const candidates = rows(diagnosis.candidates), [index, setIndex] = useState(0), candidate = candidates[index];
  const [candidatePage, setCandidatePage] = useState(0);
  const ambiguities = rows(comparison.ambiguities), gaps = [...rows(failed.gaps), ...rows(reference.gaps)];
  return <article className="debug-detail"><div className="section-heading"><h2>冻结静态报告</h2><code title={str(stored.report_ref)}>{short(stored.report_ref)}</code></div>
    <dl className="facts"><div><dt>失败 Run</dt><dd><code>{str(failed.run_id)}</code></dd></div><div><dt>成功参考</dt><dd>{reference.run_id ? <code>{str(reference.run_id)}</code> : '无参考，沿硬断言提取'}</dd></div><div><dt>证据范围</dt><dd>{diagnosis.complete_evidence === true ? '当前范围已完整核验' : '存在缺口或不确定性'}</dd></div><div><dt>原报告干预</dt><dd>未运行 · 改善未知</dd></div></dl>
    {diagnosis.status === 'insufficient_evidence' && <Notice>证据不足：以下为完整候选回退，不能宣称已定位。</Notice>}
    <h3>失败硬断言</h3>{rows(diagnosis.assertions).length ? <ul>{rows(diagnosis.assertions).map((item, i) => <li key={i}><code>{str(item.code)}</code> · {item.origin === 'research_validator' ? '固定研究验收器' : 'Runtime 停止'} <EventLink event={item.event} openEvent={openEvent} /></li>)}</ul> : <p>没有已核验硬断言。</p>}
    <div className="debug-layout"><nav className="debug-outline" aria-label="故障候选">{candidates.slice(candidatePage, candidatePage + 50).map((item, offset) => { const i = candidatePage + offset; return <button key={str(object(item.event).run_id) + ':' + String(object(item.event).seq)} className={i === index ? 'selected' : ''} aria-pressed={i === index} onClick={() => setIndex(i)}><span>排名 {String(item.rank)}{Number(item.tied_count) > 1 ? ` · ${String(item.tied_count)} 项并列` : ''}</span><strong>{str(item.action_step) || KINDS[str(item.kind)] || '模型输出'}</strong><span className="small muted">{KINDS[str(item.kind)] ?? str(item.kind)}</span></button>; })}{candidates.length > 50 && <div className="pagination"><span>共 {candidates.length} 项</span><button disabled={!candidatePage} onClick={() => { setCandidatePage(candidatePage - 50); setIndex(candidatePage - 50); }}>上一批候选</button><button disabled={candidatePage + 50 >= candidates.length} onClick={() => { setCandidatePage(candidatePage + 50); setIndex(candidatePage + 50); }}>下一批候选</button></div>}</nav>
      {candidate ? <section className="debug-candidate" aria-labelledby="candidate-title"><h3 id="candidate-title">候选决策证据</h3><EventLink event={candidate.event} openEvent={openEvent} /><p className="small muted">{str(candidate.action_provider) || '未核验唯一接受动作'} · 并列项不使用时间或 ID 打破排名。</p><TextList value={candidate.reasons} /><h3>不确定性</h3>{Array.isArray(candidate.uncertainties) && candidate.uncertainties.length ? <TextList value={candidate.uncertainties} /> : <p>当前候选没有额外标注；仍须通过独立干预验证效果。</p>}
        <h3>支持事件</h3><div className="event-links">{rows(candidate.supporting_events).map((event, i) => <EventLink key={i} event={event} openEvent={openEvent} />)}</div><details><summary>静态特征原文</summary><JsonView value={candidate.features} /></details>
        <CandidateExperiment key={String(object(candidate.event).event_id)} reportId={str(stored.debug_report_id)} candidate={candidate} openEvent={openEvent} openExperiment={openExperiment} />
      </section> : <Empty title="没有可展示候选">请查看硬断言及证据缺口；不会生成虚构根因。</Empty>}
    </div><h3>行为对齐与证据缺口</h3><p>{reference.run_id ? comparison.status === 'comparable' ? `依赖对齐：${rows(comparison.matches).length} 对；歧义 ${ambiguities.length} 组。` : '没有共同版本祖先，无法作依赖对齐。' : '未提供成功参考，不能展示两侧对齐。'} 两侧范围合计缺口 {gaps.length} 项。</p>
    {reference.run_id && <><p>首个可见分歧来自依赖图最小前沿，可有多个；它不是故障根因。</p><div className="event-links">{rows(comparison.first_left).map((item, i) => <EventLink key={'L' + i} event={item} openEvent={openEvent} label={`失败侧分歧 · seq ${String(item.seq)}`} />)}{rows(comparison.first_right).map((item, i) => <EventLink key={'R' + i} event={item} openEvent={openEvent} label={`参考侧分歧 · seq ${String(item.seq)}`} />)}</div></>}
    <EvidenceDetails title="已对齐事件" items={rows(comparison.matches)} openEvent={openEvent} mode="pair" />
    <EvidenceDetails title="仅失败侧出现" items={rows(comparison.left_only)} openEvent={openEvent} />
    <EvidenceDetails title="仅参考侧出现" items={rows(comparison.right_only)} openEvent={openEvent} />
    <EvidenceDetails title="对齐歧义" items={ambiguities} openEvent={openEvent} mode="ambiguity" />
    <EvidenceDetails title="证据缺口" items={gaps} openEvent={openEvent} mode="gap" />
    <h3>输入与工具异常</h3><EvidenceDetails title="异常观测" items={rows(diagnosis.observed_anomalies)} openEvent={openEvent} /><p className="small muted">异常观测与候选决策分别展示；不能把最早异常输入直接称为可修复决策。</p>
    <h3>环境变化</h3>{Array.isArray(report.environment_changes) && report.environment_changes.length ? <TextList value={report.environment_changes} /> : <p>没有记录到环境变化。</p>}
    <TextList value={diagnosis.limitations} /><RawDetails title="冻结报告完整原文" value={report} />
  </article>;
}

function CandidateExperiment({ reportId, candidate, openEvent, openExperiment }: { reportId: string; candidate: JsonObject; openEvent: OpenEvent; openExperiment: OpenExperiment }) {
  const [validator, setValidator] = useState(''), [experiment, setExperiment] = useState(''), [context, setContext] = useState<JsonObject | null>(null), [view, setView] = useState<JsonObject | null>(null);
  const action = useAction(), event = evidenceTarget(candidate.event), observed = object(view?.experiment);
  const [contextLocked, setContextLocked] = useState(false);
  return <div className="debug-experiment"><h3>独立干预证据</h3><p>先核对决策前检查点，显式保存单项干预，再到 A/B 表单填写预算并提交。查询报告和准备上下文不会启动试验。</p>
    <form className="inline-form" onSubmit={eventForm => { eventForm.preventDefault(); void action.run(async () => {
      setContext(null); if (event) setContext(await call(window.agentgit.getDebugCandidateContext({ reportId, candidateEventId: event.eventId, validatorRef: digest(validator, '验收器摘要') })));
    }); }}><label className="grow">候选验收器摘要<input value={validator} disabled={contextLocked} onChange={event => { setValidator(event.target.value); setContext(null); }} spellCheck={false} /></label><button disabled={action.busy || contextLocked || !validator.trim() || !event}>核对干预上下文</button></form>
    {context && <details open><summary>已核对的调用与检查点</summary><EventLink event={context.request_event} openEvent={openEvent} /><JsonView value={context.context} /></details>}
    {context && <CandidatePatch context={object(context.context)} candidate={candidate} lock={setContextLocked} openExperiment={openExperiment} />}
    <form className="inline-form" onSubmit={eventForm => { eventForm.preventDefault(); void action.run(async () => {
      setView(null); if (event) setView(await call(window.agentgit.getDebugCandidateExperiment({ reportId, candidateEventId: event.eventId, experimentId: identifier(experiment, '实验 ID') })));
    }); }}><label className="grow">候选 Experiment UUID<input value={experiment} onChange={event => { setExperiment(event.target.value); setView(null); }} spellCheck={false} /></label><button disabled={action.busy || !experiment.trim() || !event}>核对实验效果</button></form><Notice>{action.error}</Notice>
    {view && <><p role="status">{view.effect_status === 'assessed' ? `两组均已判定 · 成功率差 ${exact(view.success_rate_difference)}` : '试验尚未完整判定 · 改善未知'}</p><ExperimentStatistics statistics={object(observed.statistics)} />
      <div className="table-scroll"><table><caption>关联的独立试验（所有状态均保留）</caption><thead><tr><th>分组 / 序号</th><th>状态</th><th>判定</th><th>Run</th></tr></thead><tbody>{rows(observed.trials).map(item => <tr key={str(item.job_id)}><td>{str(item.group)} · {Number(item.index) + 1}</td><td>{str(item.status)}</td><td>{str(item.verdict)}</td><td><code>{str(item.run_id)}</code></td></tr>)}</tbody></table></div><TextList value={view.limitations} /><RawDetails title="冻结设置、干预与试验原文" value={view} /></>}
  </div>;
}

function CandidatePatch({ context, candidate, lock, openExperiment }: { context: JsonObject; candidate: JsonObject; lock: (value: boolean) => void; openExperiment: OpenExperiment }) {
  const [operator, setOperator] = useState<'PLAN_PATCH' | 'PROMPT_PATCH'>('PLAN_PATCH'), [content, setContent] = useState(''), [rationale, setRationale] = useState('');
  const [locked, setLocked] = useState(false), [createdId, setCreatedId] = useState('');
  const attempt = useRef<Inputs['createIntervention'] | null>(null), action = useAction();
  return <section className="candidate-patch"><h3>保存单项候选干预</h3><form onSubmit={event => { event.preventDefault(); void action.run(async () => {
    if (!attempt.current) { attempt.current = { request: candidatePatch(context, candidate, operator, content, rationale), key: crypto.randomUUID() }; setLocked(true); lock(true); }
    const reply = await window.agentgit.createIntervention(attempt.current);
    if (!reply.ok) { if (knownRejection(reply.error.code)) { attempt.current = null; setLocked(false); lock(false); } throw new Error(`${reply.error.message}（${reply.error.code}）`); }
    setCreatedId(identifier(str(reply.data.intervention_id), '已创建干预 ID'));
  }); }}><fieldset disabled={locked || action.busy}><legend>唯一修改的决策条件</legend><label>候选干预类型<select value={operator} onChange={event => { const kind = event.target.value as typeof operator; setOperator(kind); setContent(kind === 'PROMPT_PATCH' ? str(object(context.config).prompt) : ''); }}><option value="PLAN_PATCH">计划（完整 JSON）</option><option value="PROMPT_PATCH">完整 Prompt</option></select></label><label>{operator === 'PLAN_PATCH' ? '候选完整计划 JSON' : '候选完整 Prompt'}<textarea rows={6} className="mono" value={content} onChange={event => setContent(event.target.value)} spellCheck={false} /></label><label>候选干预理由<textarea rows={3} maxLength={2000} value={rationale} onChange={event => setRationale(event.target.value)} /></label></fieldset><button disabled={action.busy || Boolean(createdId)}>{createdId ? '单项干预已保存' : locked ? '继续原干预请求' : '保存候选干预'}</button></form><Notice>{action.error}</Notice>
    {locked && !createdId && <p className="small muted">保存结果未确认；原内容与幂等键保持，重试不会创建另一干预。</p>}
    {createdId && <><label>已创建候选干预 ID<input readOnly value={createdId} /></label><button onClick={() => void action.run(async () => { const validator = await call(window.agentgit.getValidator({ validatorRef: str(object(context.precondition).validator_ref) })); openExperiment(candidatePreset(context, validator, createdId)); })} disabled={action.busy}>前往 A/B 表单</button><p className="small muted">沿用原检查点、验收器与样例划分；A 保持来源条件，B 预填这一项干预，仍需显式提交预算和试验。</p></>}
  </section>;
}

function TextList({ value }: { value: Json | undefined }) { return <ul>{Array.isArray(value) ? value.map((item, i) => <li key={i}>{str(item)}</li>) : null}</ul>; }

function RawDetails({ title, value }: { title: string; value: Json }) {
  const [open, setOpen] = useState(false);
  return <details onToggle={event => setOpen(event.currentTarget.open)}><summary>{title}</summary>{open && <JsonView value={value} />}</details>;
}

function EvidenceDetails({ title, items, openEvent, mode = 'event' }: { title: string; items: JsonObject[]; openEvent: OpenEvent; mode?: 'event' | 'pair' | 'gap' | 'ambiguity' }) {
  const [open, setOpen] = useState(false), [after, setAfter] = useState(0);
  return <details onToggle={event => setOpen(event.currentTarget.open)}><summary>{title}（{items.length} 项）</summary>{open && <><div>{items.slice(after, after + 50).map((item, i) => <div className="debug-evidence-row" key={after + i}>{mode === 'ambiguity' ? <><p>重复身份未消歧</p>{[...rows(item.left), ...rows(item.right)].map((ref, j) => <EventLink key={j} event={ref} openEvent={openEvent} />)}</> : mode === 'pair' ? <><span>{str(item.comparison)} · {str(item.basis)} </span><EventLink event={item.left} openEvent={openEvent} label={`失败侧 seq ${String(object(item.left).seq)}`} /><EventLink event={item.right} openEvent={openEvent} label={`参考侧 seq ${String(object(item.right).seq)}`} /><JsonView value={item.differences} /></> : <>{mode === 'gap' && <code>{str(item.reason)}</code>}<EventLink event={mode === 'gap' ? item.event : item} openEvent={openEvent} /></>}</div>)}</div>{items.length > 50 && <div className="pagination"><span>{after + 1}–{Math.min(after + 50, items.length)} / {items.length}</span><button disabled={!after} onClick={() => setAfter(after - 50)}>上一批{title}</button><button disabled={after + 50 >= items.length} onClick={() => setAfter(after + 50)}>下一批{title}</button></div>}</>}</details>;
}
