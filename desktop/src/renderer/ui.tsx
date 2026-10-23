import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { Json, JsonObject, Reply, DesktopApi } from '../shared/bridge';

declare global { interface Window { agentgit: DesktopApi } }
export function object(value: Json | undefined): JsonObject { return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
export function rows(value: Json | undefined): JsonObject[] { return Array.isArray(value) ? value.map(object) : []; }
export function str(value: Json | undefined): string { return typeof value === 'string' ? value : ''; }
export function short(value: Json | undefined): string { const text = str(value); return text ? text.slice(0, 12) : '—'; }
export function pretty(value: Json | undefined): string { return value === undefined ? '—' : JSON.stringify(value, null, 2); }
export function date(value: Json | undefined): string { const text = str(value); return text ? new Date(text).toLocaleString('zh-CN', { hour12: false }) : '—'; }
export function err(error: unknown): string { return error instanceof Error ? error.message : '操作失败，请重新查询任务状态'; }
export async function call<T>(request: Promise<Reply<T>>): Promise<T> {
  const reply = await request;
  if (!reply.ok) throw new Error(`${reply.error.message}（${reply.error.code}）`);
  return reply.data;
}
export function useRead<T>(read: () => Promise<T>, deps: React.DependencyList, interval = 0) {
  const [data, setData] = useState<T | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true, timer: ReturnType<typeof setTimeout> | undefined;
    setData(null); setError(''); setLoading(true);
    async function load() {
      try { const next = await read(); if (active) { setData(next); setError(''); } }
      catch (failure) { if (active) setError(err(failure)); }
      finally { if (active) { setLoading(false); if (interval) timer = setTimeout(() => void load(), interval); } }
    }
    void load();
    return () => { active = false; clearTimeout(timer); };
  }, [...deps, revision]);
  return { data, error, loading, refresh: useCallback(() => setRevision(value => value + 1), []) };
}
export function useAction() {
  const guard = useRef(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function run(work: () => Promise<void>) {
    if (guard.current) return;
    guard.current = true; setBusy(true); setError('');
    try { await work(); } catch (failure) { setError(err(failure)); }
    finally { guard.current = false; setBusy(false); }
  }
  return { busy, error, run };
}
const STATUS: Record<string, string> = { queued: '排队中', running: '运行中', completed: '已完成', failed: '失败',
  cancelled: '已取消', paused: '已暂停', waiting_approval: '等待审批', outcome_unknown: '结果未知', interrupted: '已中断',
  pending: '待处理', approved: '已批准', rejected: '已拒绝', expired: '已过期' };
export function Status({ value }: { value: Json | undefined }) {
  const state = str(value);
  return <span className={`status status-${state}`}><span aria-hidden="true" />{(STATUS[state] ?? state) || '未知'}</span>;
}
export function Notice({ children }: { children?: React.ReactNode }) { return children ? <div className="notice" role="alert">{children}</div> : null; }
export function Empty({ title, children }: { title: string; children?: React.ReactNode }) { return <div className="empty"><h2>{title}</h2><p>{children}</p></div>; }
export function JsonView({ value }: { value: Json | undefined }) { return <pre className="code" tabIndex={0}>{pretty(value)}</pre>; }
export type PageProps = { runId: string; setRunId: (value: string) => void; openTimeline: (id: string) => void; openBranch: (id: string, commit: string) => void; selectedCommit: string; setSelectedCommit: (id: string) => void };
