import type { JsonObject } from './bridge';

function object(value: unknown): JsonObject { return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}; }
function text(value: unknown): string { return typeof value === 'string' ? value : ''; }

export function fixedProgramKind(run: JsonObject | null | undefined): string { return text(object(run?.adapter).kind); }
export function fixedProgramOrigin(run: JsonObject): string {
  const adapter = object(run.adapter);
  return text(object(adapter.recording).source_run_id) || text(object(adapter.fork).source_run_id) || text(adapter.source_run_id) || text(run.origin_run_id);
}
export function fixedProgramTitle(run: JsonObject): string {
  const kind = fixedProgramKind(run);
  return kind === 'collaboration_role/v1' ? '四角色协作' : kind === 'langgraph_program/v1' ? 'LangGraph 固定图' : kind === 'mcp_single_call/v1' ? 'MCP 固定工具' : '适配器任务';
}
export function nativeNext(adapter: JsonObject): string {
  if (!text(adapter.native_ref)) return '尚无原生检查点';
  if (!Array.isArray(adapter.native_next)) return '未知';
  if (!adapter.native_next.length) return '无后续节点';
  const labels: Record<string, string> = { __start__: '启动', fetch: '取证', review: '审阅', publish: '发布' };
  return adapter.native_next.map(value => (labels[text(value)] ?? text(value)) || '未知节点').join(' → ');
}
