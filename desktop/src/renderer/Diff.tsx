import React, { useState } from 'react';
import type { JsonObject } from '../shared/bridge';
import { call, Empty, JsonView, Notice, object, rows, short, str, useRead } from './ui';
import type { EvidenceTarget } from './ui';
import DebugReports from './DebugReports';
import type { CandidateExperimentPreset } from '../shared/diagnosis';

export default function Diff({ diagnosisRunId, openEvent, openExperiment }: { diagnosisRunId: string; openEvent: (target: EvidenceTarget) => void; openExperiment: (preset: CandidateExperimentPreset) => void }) {
  const [view, setView] = useState<'structure' | 'diagnosis'>(diagnosisRunId ? 'diagnosis' : 'structure');
  const [base, setBase] = useState(''), [head, setHead] = useState(''), [leftBranch, setLeftBranch] = useState('main'), [rightBranch, setRightBranch] = useState('main');
  const [selection, setSelection] = useState<{ base: string; head: string } | null>(null), [after, setAfter] = useState(0), [index, setIndex] = useState(0);
  const branches = useRead(() => call(window.agentgit.listBranches({})), []);
  const left = useRead(() => call(window.agentgit.listCommits({ branchName: leftBranch, limit: 100 })), [leftBranch]);
  const right = useRead(() => call(window.agentgit.listCommits({ branchName: rightBranch, limit: 100 })), [rightBranch]);
  const diff = useRead(() => selection ? call(window.agentgit.getDiff({ ...selection, after, limit: 50 })) : Promise.resolve({} as JsonObject), [selection, after]);
  const changes = rows(diff.data?.items), selected = changes[index];
  return <><div className="page-heading"><div><span className="eyebrow">DIFF</span><h1>差异</h1><p>比较两个检查点的客观状态、调用与产物。</p></div></div>
    <div className="tabs" role="tablist" aria-label="比较类型"><button role="tab" aria-selected={view === 'structure'} onClick={() => setView('structure')}>结构化差异</button><button role="tab" aria-selected={view === 'diagnosis'} onClick={() => setView('diagnosis')}>故障定位</button></div>
    {view === 'diagnosis' ? <DebugReports failedRunId={diagnosisRunId} openEvent={openEvent} openExperiment={openExperiment} /> : <>
    <form className="diff-select panel" onSubmit={event => { event.preventDefault(); setSelection({ base: base.trim(), head: head.trim() }); setAfter(0); setIndex(0); }}>
      <CommitSelect label="BASE · 基线" branch={leftBranch} setBranch={setLeftBranch} value={base} setValue={setBase} branches={rows(branches.data?.items)} commits={rows(left.data?.items)} listId="diff-base" />
      <span className="diff-arrow" aria-hidden="true">→</span><CommitSelect label="HEAD · 对照" branch={rightBranch} setBranch={setRightBranch} value={head} setValue={setHead} branches={rows(branches.data?.items)} commits={rows(right.data?.items)} listId="diff-head" />
      <button className="primary" disabled={!base.trim() || !head.trim() || diff.loading}>比较</button>
    </form><Notice>{branches.error || left.error || right.error || diff.error}</Notice>
    {!selection ? <Empty title="选择两个检查点">支持同项目不同分支，也可粘贴完整 Commit 摘要。比较不会修改历史。</Empty> : diff.loading ? <p role="status">正在比较检查点…</p> : diff.data && <><div className="diff-summary"><strong>{String(diff.data.total_changes ?? 0)} 项差异</strong><span>共同祖先 <code title={str(diff.data.common_ancestor)}>{diff.data.common_ancestor ? short(diff.data.common_ancestor) : '无共同祖先'}</code></span><span className="muted small">仅呈现结构化事实，不生成因果置信度。</span></div>
      {!changes.length ? <Empty title="所选版本没有差异">返回范围内没有结构化变更。</Empty> : <div className="diff-layout"><nav className="diff-outline" aria-label="差异路径">{changes.map((item, itemIndex) => <button className={index === itemIndex ? 'selected' : ''} key={after + itemIndex} onClick={() => setIndex(itemIndex)}><span className="change-kind">{str(item.operation) || str(item.kind)}</span><code>{str(item.path) || str(item.pointer) || `变更 ${after + itemIndex + 1}`}</code></button>)}</nav><section className="diff-detail"><h2 className="mono">{str(selected?.path) || str(selected?.pointer) || '结构化变更'}</h2><p className="small muted">JSON Pointer 与数组索引按后端原值显示；二进制产物以摘要/版本引用比较。</p><div className="diff-columns"><div><h3>变更前 · BASE</h3><JsonView value={selected?.before} /></div><div><h3>变更后 · HEAD</h3><JsonView value={selected?.after} /></div></div><details><summary>完整差异证据</summary><JsonView value={selected} /></details><p className="small muted">{str(object(selected?.metadata).media_type)}</p></section></div>}
      <div className="pagination"><span className="small muted">当前从 {after + 1} 项起，每页最多 50 项</span><button disabled={!after} onClick={() => { setAfter(0); setIndex(0); }}>回到首项</button><button disabled={!diff.data.has_more} onClick={() => { setAfter(Number(diff.data?.next_after)); setIndex(0); }}>更多差异</button></div></>}
    </>}
  </>;
}
function CommitSelect({ label, branch, setBranch, value, setValue, branches, commits, listId }: { label: string; branch: string; setBranch: (value: string) => void; value: string; setValue: (value: string) => void; branches: JsonObject[]; commits: JsonObject[]; listId: string }) {
  return <div className="commit-select"><label>{label}<select value={branch} onChange={event => setBranch(event.target.value)}>{branches.map(item => <option key={str(item.name)}>{str(item.name)}</option>)}</select></label><label className="sr-only" htmlFor={listId + '-input'}>{label} Commit</label><input id={listId + '-input'} value={value} onChange={event => setValue(event.target.value)} list={listId} placeholder="完整 Commit 摘要" spellCheck={false} /><datalist id={listId}>{commits.map(item => <option value={str(item.commit_id)} key={str(item.commit_id)} />)}</datalist></div>;
}
