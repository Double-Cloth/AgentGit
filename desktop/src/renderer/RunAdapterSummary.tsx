import React, { useId } from 'react';
import type { Json, JsonObject } from '../shared/bridge';
import { fixedProgramKind, fixedProgramOrigin, fixedProgramTitle, nativeNext } from '../shared/fixed-program';
import { integer } from '../shared/model-config';
import { JsonView, object, short, str } from './ui';

function count(value: Json | undefined): string { const n = integer(value); return n === undefined ? '未知' : n.toLocaleString('zh-CN'); }

export default function RunAdapterSummary({ run, openSource }: { run: JsonObject; openSource: (runId: string) => void }) {
  const id = useId(), adapter = object(run.adapter), kind = fixedProgramKind(run);
  if (!kind) return null;
  const graph = kind === 'langgraph_program/v1', origin = fixedProgramOrigin(run);
  const usage = object(adapter.logical_usage), recording = object(adapter.recording), fork = object(adapter.fork);
  if (kind === 'collaboration_role/v1') return <section className="budget-summary adapter-summary" aria-labelledby={id}><h2 id={id}>四角色协作 · {str(adapter.role)}</h2>
    <p>协作 <code>{str(adapter.collaboration_id)}</code> · 当前阶段 {str(adapter.phase)}。下方运行控制只作用于本角色；整组控制请回到运行页的四角色协作。</p>
    <dl className="facts"><div><dt>逻辑模型 / 工具 / 步数</dt><dd>{count(usage.model_calls)} / {count(usage.tool_calls)} / {count(usage.steps)}</dd></div><div><dt>本次物理调用意图</dt><dd>{count(adapter.physical_call_intents)}</dd></div><div><dt>复用来源调用</dt><dd>{count(adapter.reused_calls)}</dd></div><div><dt>未知调用</dt><dd>{count(adapter.unknown_calls)}</dd></div></dl>
    <p className="small muted">物理意图是尝试登记，不等于已执行次数或账单。Mock 用量按 UTF-8 字节记。Recorded 保留历史逻辑用量，本次执行与复用分别核对。</p>
    {origin && <button onClick={() => openSource(origin)}>打开来源角色运行</button>}
    <details><summary>角色预算、权限与来源原文</summary><JsonView value={adapter} /></details>
  </section>;
  return <section className="budget-summary adapter-summary" aria-labelledby={id}><h2 id={id}>{fixedProgramTitle(run)}</h2>
    <p className="small muted">{adapter.has_model === false ? '无模型调用' : '模型范围见原文'} · {str(adapter.server_identity) || '工具来源待核对'} / {str(adapter.tool_name) || '工具待核对'} · 本地效果 {str(adapter.effect_class) || '未知'}</p>
    <dl className="facts"><div><dt>逻辑步骤</dt><dd>{count(usage.steps)}</dd></div><div><dt>逻辑工具用量</dt><dd>{count(usage.tool_calls)}</dd></div><div><dt>本次工具尝试</dt><dd>{count(adapter.actual_attempts)}</dd></div>
      {graph && <><div><dt>后续节点</dt><dd>{nativeNext(adapter)}</dd></div><div><dt>框架版本</dt><dd>LangGraph {str(adapter.framework_version) || '未知'}</dd></div></>}
    </dl>
    {run.mode === 'recorded' ? <p>Recorded 消费固定来源范围的录制并重算逻辑状态与报告，不调用真实工具或启动 Server。历史逻辑用量保留，本次尝试单列；原生物理 ID 单独保存。</p>
      : Object.keys(fork).length ? <p>在独立图中更新审阅备注，复用已保存的取证；来源运行和原分支保持不变。</p>
      : <p className="small muted">固定程序由后端操作者提交。控制请求在安全边界落实；未知尝试不记作零，也不自动重发。</p>}
    {graph && <p className="small muted">{str(adapter.template)} · 原生检查点 <code title={str(adapter.native_ref)}>{short(adapter.native_ref)}</code>{Object.keys(fork).length ? ` · 备注恢复${adapter.fork_initialized ? '已完成' : '待完成'}` : ''}</p>}
    {origin && <button onClick={() => openSource(origin)}>打开来源运行</button>}
    <details className="budget-details"><summary>适配器、范围与调用来源原文</summary><JsonView value={{ ...adapter, recording: Object.keys(recording).length ? recording : null, origin_run_id: run.origin_run_id ?? null }} /></details>
  </section>;
}
