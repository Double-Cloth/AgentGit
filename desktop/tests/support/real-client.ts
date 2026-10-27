import { expect, type Page } from '@playwright/test';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';
import type { JsonObject, Reply } from '../../src/shared/bridge';

export const TOKEN = 'fixture-real-desktop-token-000000000000000000000';
export function data<T>(reply: Reply<T>): T { if (!reply.ok) throw new Error(`${reply.error.code}: ${reply.error.message}`); return reply.data; }
export function objects(value: unknown): JsonObject[] { return Array.isArray(value) ? value as JsonObject[] : []; }
export async function startBackend(kind: 'standard' | 'repair' | 'repair-boundaries' | 'repair-observation' = 'standard') {
  const script = kind === 'repair-observation' ? 'repair_observation_backend.py' : kind === 'repair-boundaries' ? 'repair_boundaries_backend.py' : kind === 'repair' ? 'repair_backend.py' : 'real_backend.py';
  const child = spawn('uv', ['run', '--locked', 'python', `desktop/tests/support/${script}`], {
    cwd: resolve('..'), windowsHide: true, env: { ...process.env, AGENTGIT_API_TOKEN: TOKEN, PYTHONIOENCODING: 'utf-8' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let logs = '', sequence = 0, terminal: Error | null = null;
  const diagnostics = () => logs.replace(/^.*"event": "http.request".*\r?\n/gm, '');
  child.stderr.on('data', bytes => { logs = (logs + String(bytes)).slice(-64000); });
  const pending = new Map<number, { resolve: (value: JsonObject) => void; reject: (error: Error) => void }>();
  let readyResolve: (value: { url: string; schema: string; root: string }) => void, readyReject: (error: Error) => void;
  const ready = new Promise<{ url: string; schema: string; root: string }>((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const finished = new Promise<number | null>(resolve => child.once('exit', resolve));
  createInterface({ input: child.stdout }).on('line', line => {
    try { const message = JSON.parse(line); if (message.ready) readyResolve(message);
      else if (pending.has(message.id)) { pending.get(message.id)!.resolve(message.result); pending.delete(message.id); }
    } catch { readyReject(new Error('联调进程输出无效')); }
  });
  child.once('error', error => readyReject(error));
  child.once('exit', code => { const error = new Error(`联调进程结束 ${code}\n${diagnostics()}`); terminal = error; readyReject(error); for (const value of pending.values()) value.reject(error); pending.clear(); });
  child.stdin.on('error', () => { const error = terminal ?? new Error(`联调控制通道已关闭\n${diagnostics()}`); for (const value of pending.values()) value.reject(error); pending.clear(); });
  const address = await ready;
  return { ...address,
    command: (command: string, args: JsonObject = {}) => new Promise<JsonObject>((resolve, reject) => {
      if (terminal || child.stdin.destroyed) { reject(terminal ?? new Error('联调控制通道已关闭')); return; }
      const id = ++sequence; pending.set(id, { resolve, reject }); child.stdin.write(JSON.stringify({ id, command, ...args }) + '\n');
    }),
    stop: async () => { if (!terminal && !child.stdin.destroyed) child.stdin.end(JSON.stringify({ command: 'stop' }) + '\n'); expect(await finished, diagnostics()).toBe(0); },
  };
}
export async function readRun(page: Page, runId: string) { return data(await page.evaluate(id => window.agentgit.getRun({ runId: id }), runId)); }
export async function completed(page: Page, runId: string) {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    const state = await readRun(page, runId);
    if (state.status === 'completed') return state;
    if (['failed', 'cancelled', 'budget_exhausted', 'outcome_unknown'].includes(String(state.status))) throw new Error(`Run 非成功终态：${JSON.stringify(state)}`);
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error('真实后端运行未在期限内完成');
}
