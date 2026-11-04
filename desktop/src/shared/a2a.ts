import type { JsonObject } from './bridge';

export function a2aServer(endpoint: string, server: string, principal: string): JsonObject {
  if (!/^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})$/.test(endpoint) || Number(endpoint.split(':').at(-1)) > 65535) throw new Error('请填写明确的回环根地址，例如 http://127.0.0.1:8123');
  if (![server, principal].every(value => /^[A-Za-z0-9_-]{1,64}$/.test(value))) throw new Error('服务名称与主体须为1至64个英文字母、数字、下划线或连字符');
  return { schema_version: 1, endpoint, server_identity: server, principal_id: principal, timeout_ms: 2000, max_response_bytes: 65536, protocol_version: '1.0', upstream_release: 'v1.0.1' };
}

export function a2aProgram(binding: JsonObject, texts: string[], messageId: () => string): JsonObject {
  if (binding.schema_version !== 1 || !binding.server || !binding.card_json) throw new Error('请先发现并核对服务卡片');
  if (!texts.length || texts.length > 4 || texts.some(text => !text.trim() || text.length > 4000)) throw new Error('预声明1至4条非空公开消息，每条最多4000字符');
  return { schema_version: 1, kind: 'fixed_a2a_task/v1', binding,
    messages: texts.map(text => JSON.stringify({ configuration: { acceptedOutputModes: ['text/plain', 'application/json'], returnImmediately: true }, message: { messageId: messageId(), parts: [{ mediaType: 'text/plain', text }], role: 'ROLE_USER' } })),
    max_polls: 16, poll_interval_ms: 1000 };
}

export function a2aRemote(value: unknown): string {
  const labels: Record<string, string> = { TASK_STATE_SUBMITTED: '已提交', TASK_STATE_WORKING: '处理中', TASK_STATE_INPUT_REQUIRED: '需要补充输入', TASK_STATE_AUTH_REQUIRED: '需要认证', TASK_STATE_COMPLETED: '已完成', TASK_STATE_FAILED: '失败', TASK_STATE_CANCELED: '已取消', TASK_STATE_REJECTED: '已拒绝' };
  return typeof value === 'string' && value ? labels[value] ?? value : '尚无确认回执';
}

export function a2aCancel(value: unknown): string {
  const labels: Record<string, string> = { not_requested: '未请求', not_sent: '未发送', pending_approval: '等待独立审批', confirmed: '已确认', rejected: '已拒绝', not_confirmed: '尚未确认', outcome_unknown: '结果未知' };
  return typeof value === 'string' ? labels[value] ?? '未知' : '未知';
}
