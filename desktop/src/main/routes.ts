import { createHash } from 'node:crypto';
import type { BinaryChunk, ConnectionState, Json, JsonObject } from '../shared/bridge';
import { ApiClient, DesktopError, normalizeBaseUrl, type ApiRequest } from './api-client';

type Args = Record<string, unknown>;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DIGEST = /^[0-9a-f]{64}$/;
const RESOURCES = ['command', 'code', 'stdin', 'stdout', 'stderr', 'metadata', 'input_manifest', 'output_manifest'];
const REQUIRED_FEATURES = ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff'];

export function fields(value: unknown, required: string[], optional: string[] = []): Args {
  if (value === undefined) value = {};
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || ![null, Object.prototype].includes(Object.getPrototypeOf(value) as object | null)) {
    throw new DesktopError('validation', '业务参数必须是普通对象');
  }
  const data = value as Args;
  if (required.some(key => !Object.hasOwn(data, key))
      || Object.keys(data).some(key => !required.includes(key) && !optional.includes(key))) {
    throw new DesktopError('validation', '业务参数缺失或包含未授权字段');
  }
  return data;
}
function text(value: unknown, max = 200): string {
  if (typeof value !== 'string' || !value.trim() || value.length > max || value.includes('\0')) {
    throw new DesktopError('validation', '文本字段无效');
  }
  return value;
}
function id(value: unknown): string {
  const result = text(value, 36);
  if (!UUID.test(result)) throw new DesktopError('validation', '资源标识无效');
  return result.toLowerCase();
}
function digest(value: unknown): string {
  const result = text(value, 64);
  if (!DIGEST.test(result)) throw new DesktopError('validation', '检查点摘要无效');
  return result;
}
function number(value: unknown, fallback: number, max = 1000, min = 0): number {
  if (value === undefined) return fallback;
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) {
    throw new DesktopError('validation', '整数参数越界');
  }
  return value;
}
function key(value: unknown): string {
  const result = text(value);
  if (!/^[a-zA-Z0-9._:-]+$/.test(result)) throw new DesktopError('validation', '幂等键格式无效');
  return result;
}
export function jsonObject(value: unknown): JsonObject {
  let nodes = 0;
  const seen = new Set<object>();
  function check(item: unknown, depth: number): void {
    if (++nodes > 100000 || depth > 64) throw new DesktopError('output_limit', '业务内容过于复杂');
    if (item === null || typeof item === 'boolean') return;
    if (typeof item === 'number' && Number.isSafeInteger(item)) return;
    if (typeof item === 'string') {
      for (const char of item) {
        const point = char.codePointAt(0)!;
        if (point >= 0xd800 && point <= 0xdfff) throw new DesktopError('validation', '文本包含无效 Unicode');
      }
      return;
    }
    if (item && typeof item === 'object') {
      if (seen.has(item)) throw new DesktopError('validation', '业务内容不能包含循环引用');
      if (!Array.isArray(item) && ![null, Object.prototype].includes(Object.getPrototypeOf(item) as object | null)) {
        throw new DesktopError('validation', '不支持该业务内容类型');
      }
      seen.add(item);
      if (Array.isArray(item)) for (const child of item) check(child, depth + 1);
      else for (const [name, child] of Object.entries(item)) { check(name, depth + 1); check(child, depth + 1); }
      seen.delete(item); return;
    }
    throw new DesktopError('validation', '业务内容不是受支持的 JSON');
  }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new DesktopError('validation', '业务内容必须是 JSON 对象');
  check(value, 0);
  if (Buffer.byteLength(JSON.stringify(value)) > 1048576) throw new DesktopError('output_limit', '业务内容超过 1 MiB');
  return value as JsonObject;
}
function query(path: string, values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(values)) if (value !== undefined) params.set(name, String(value));
  return path + (params.size ? '?' + params.toString() : '');
}

export interface Preferences { baseUrl: string; repositoryId: string | null }
export type SaveArtifact = (name: string, bytes: Buffer) => Promise<JsonObject>;

export class BusinessController {
  private client: ApiClient;
  private descriptor: JsonObject | null = null;
  private state: ConnectionState;
  private contextVersion = 0;
  constructor(preferences: Preferences, private readonly token: string | undefined,
      private readonly persist: (value: Preferences) => Promise<void>, private readonly saveArtifact: SaveArtifact) {
    this.client = new ApiClient(preferences.baseUrl, token);
    this.state = this.empty(preferences.repositoryId);
  }
  private empty(repositoryId: string | null): ConnectionState {
    return { baseUrl: this.client.baseUrl, tokenConfigured: this.client.tokenConfigured,
      repositoryId, api: 'unknown', database: 'unknown', identity: this.token ? 'unknown' : 'missing',
      compatibility: 'unknown', worker: 'unknown', sandbox: 'unknown', backendVersion: null, message: '尚未检查连接' };
  }
  cancelReads(): void { this.client.cancelReads(); }
  private connected(repository = true): string | undefined {
    if (this.state.compatibility !== 'compatible' || this.state.identity !== 'accepted') {
      throw new DesktopError('connection_required', '请先连接兼容的本地 API 并确认身份');
    }
    if (this.state.database !== 'ready') throw new DesktopError('database_unavailable', '数据库尚未就绪，请重新检查连接');
    if (repository && !this.state.repositoryId) throw new DesktopError('repository_required', '请先选择或创建项目');
    return repository ? this.state.repositoryId! : undefined;
  }
  private async probe(): Promise<ConnectionState> {
    const client = this.client;
    const version = this.contextVersion;
    let accepted: JsonObject | null = null;
    const repository = this.state.repositoryId;
    const next = this.empty(repository);
    const [health, ready] = await Promise.allSettled([
      client.json({ method: 'GET', path: '/health', publicProbe: true }),
      client.json({ method: 'GET', path: '/ready', publicProbe: true }),
    ]);
    if (health.status === 'fulfilled' && health.value.status === 'ok') {
      next.api = 'reachable';
      next.backendVersion = typeof health.value.version === 'string' ? health.value.version : null;
      next.database = ready.status === 'fulfilled' && ready.value.status === 'ready' ? 'ready' : 'unavailable';
      if (next.backendVersion !== '0.1.0') {
        next.compatibility = 'incompatible'; next.message = '后端版本不兼容，请使用当前桌面对应的后端';
      } else if (!client.tokenConfigured) {
        next.identity = 'missing'; next.message = '请预置 AGENTGIT_API_TOKEN 后重启桌面';
      } else {
        try {
          const descriptor = await client.json({ method: 'GET', path: '/capabilities' });
          if (descriptor.schema_version !== 1 || descriptor.version !== next.backendVersion || !Array.isArray(descriptor.features)
              || !REQUIRED_FEATURES.every(value => (descriptor.features as Json[]).includes(value))) {
            next.compatibility = 'incompatible'; next.message = '后端缺少当前桌面需要的只读能力';
          } else {
            accepted = descriptor; next.identity = 'accepted'; next.compatibility = 'compatible';
            next.message = next.database === 'ready' ? 'API 与数据库可用；Worker 和执行环境仍须以实际任务结果确认' : 'API 已连接，数据库尚未就绪';
          }
        } catch (error) {
          next.identity = error instanceof DesktopError && ['unauthorized', 'http_401', 'identity_invalid'].includes(error.code) ? 'rejected' : 'unknown';
          next.compatibility = 'unknown';
          next.message = error instanceof DesktopError ? error.message : '能力检查失败';
        }
      }
    } else {
      next.api = 'unreachable'; next.message = health.status === 'rejected' && health.reason instanceof DesktopError ? health.reason.message : '无法连接本地 API';
    }
    if (this.client !== client || this.contextVersion !== version) throw new DesktopError('context_changed', '连接上下文已改变');
    this.descriptor = accepted;
    this.state = next;
    return { ...next };
  }
  async handle(operation: unknown, raw: unknown): Promise<unknown> {
    if (typeof operation !== 'string') throw new DesktopError('validation', '业务操作无效');
    if (operation === 'getConnection') { fields(raw, []); return this.probe(); }
    if (operation === 'configureConnection') {
      const input = fields(raw, ['baseUrl']);
      const baseUrl = normalizeBaseUrl(input.baseUrl);
      this.cancelReads(); this.client = new ApiClient(baseUrl, this.token); this.descriptor = null;
      const version = ++this.contextVersion;
      this.state = this.empty(null);
      await this.persist({ baseUrl, repositoryId: null });
      if (this.contextVersion !== version) throw new DesktopError('context_changed', '连接上下文已改变');
      return this.probe();
    }
    if (operation === 'selectRepository') {
      const input = fields(raw, ['repositoryId']); this.connected(false);
      const repositoryId = id(input.repositoryId);
      const client = this.client, version = ++this.contextVersion;
      this.cancelReads();
      const value = await client.json({ method: 'GET', path: `/repositories/${repositoryId}`, repository: repositoryId });
      if (client !== this.client || version !== this.contextVersion) throw new DesktopError('context_changed', '项目上下文已改变');
      if (value.id !== repositoryId) throw new DesktopError('invalid_response', '项目响应归属不符');
      this.cancelReads(); this.state.repositoryId = repositoryId;
      await this.persist({ baseUrl: this.client.baseUrl, repositoryId });
      if (client !== this.client || version !== this.contextVersion) throw new DesktopError('context_changed', '项目上下文已改变');
      return { ...this.state };
    }
    if (operation === 'capabilities') {
      fields(raw, []);
      if (!this.descriptor || this.state.identity !== 'accepted') throw new DesktopError('connection_required', '请先检查连接');
      return this.descriptor;
    }
    if (['readArtifact', 'exportArtifact'].includes(operation)) {
      const input = fields(raw, ['artifactId'], operation === 'readArtifact' ? ['offset', 'limit'] : []);
      const offset = operation === 'readArtifact' ? number(input.offset, 0, 8388608) : 0;
      const limit = operation === 'readArtifact' ? number(input.limit, 65536, 1048576, 1) : 0;
      const repository = this.connected()!;
      const client = this.client, version = this.contextVersion;
      const response = await client.bytes({ method: 'GET', path: `/artifacts/${id(input.artifactId)}/content`, repository, maxBytes: 8388608 });
      if (client !== this.client || version !== this.contextVersion) throw new DesktopError('context_changed', '项目上下文已改变');
      const etag = response.headers.etag?.replaceAll('"', '') ?? '';
      const actual = createHash('sha256').update(`agentgit\0AGJSON-v1\0blob\0${response.body.length}\0`).update(response.body).digest('hex');
      if (etag !== actual) throw new DesktopError('object_corrupt', '产物内容与摘要不符');
      if (operation === 'readArtifact') {
        client.assertSafeForIpc(response.body);
        if (offset > response.body.length) throw new DesktopError('validation', '读取偏移超过产物大小');
        const end = Math.min(offset + limit, response.body.length);
        const bytes = response.body.subarray(offset, end);
        return { base64: bytes.toString('base64'), objectRef: etag, offset,
          nextOffset: end < response.body.length ? end : null, totalBytes: response.body.length } satisfies BinaryChunk;
      }
      let name = 'artifact.bin';
      const disposition = response.headers['content-disposition'];
      if (typeof disposition === 'string') {
        const match = /filename\*=UTF-8''([^;]+)/.exec(disposition);
        if (match?.[1]) { try { name = decodeURIComponent(match[1]); } catch { /* 使用安全默认名。 */ } }
      }
      name = name.replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/g, '').slice(0, 150) || 'artifact.bin';
      if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name)) name = '_' + name;
      this.client.assertSafeForIpc(Buffer.from(name));
      return this.saveArtifact(name, response.body);
    }
    const plan = this.plan(operation, raw);
    const repository = this.connected(plan.path !== '/repositories');
    const client = this.client;
    const version = this.contextVersion;
    const spec: ApiRequest = { ...plan, ...(repository ? { repository } : {}) };
    if (operation === 'getExecutionContent') {
      const response = await client.bytes(spec);
      if (client !== this.client || version !== this.contextVersion) throw new DesktopError('context_changed', '项目上下文已改变');
      const input = raw as Args;
      const offset = number(input.offset, 0, 8388608);
      const total = Number(response.headers['x-content-bytes']);
      const next = response.headers['x-next-offset'];
      const reference = response.headers.etag?.replaceAll('"', '') ?? '';
      if (!DIGEST.test(reference) || !Number.isSafeInteger(total) || total < offset + response.body.length
          || total > 8388608 || Number(response.headers['x-content-offset']) !== offset
          || (next === '' ? total !== offset + response.body.length
            : !Number.isSafeInteger(Number(next)) || Number(next) !== offset + response.body.length || Number(next) >= total)) {
        throw new DesktopError('invalid_response', '执行证据分段元数据无效');
      }
      client.assertSafeForIpc(response.body);
      return { base64: response.body.toString('base64'), objectRef: reference, offset,
        totalBytes: total, nextOffset: next === '' ? null : Number(next) } satisfies BinaryChunk;
    }
    const value = await client.json(spec);
    if (plan.method === 'GET' && (this.client !== client || this.contextVersion !== version || this.state.repositoryId !== repository)) {
      throw new DesktopError('context_changed', '读取期间项目上下文已改变');
    }
    return value;
  }
  private plan(operation: string, raw: unknown): ApiRequest {
    let input: Args;
    const get = (path: string, maxBytes = 8388608): ApiRequest => ({ method: 'GET', path, maxBytes });
    const post = (path: string, body: JsonObject): ApiRequest => ({ method: 'POST', path, body: { schema_version: 1, ...body }, key: key(input.key) });
    const page = (path: string) => get(query(path, { after_id: input.afterId === undefined ? undefined : id(input.afterId), limit: number(input.limit, 30, 100, 1) }));
    switch (operation) {
      case 'listRuns': input = fields(raw, [], ['afterId', 'limit']); return page(`/repositories/${this.state.repositoryId ?? ''}/runs`);
      case 'getRun': input = fields(raw, ['runId']); return get(`/runs/${id(input.runId)}`);
      case 'getJob': case 'getReplay': input = fields(raw, ['jobId']); return get(`/${operation === 'getJob' ? 'jobs' : 'replays'}/${id(input.jobId)}`);
      case 'getCommit': input = fields(raw, ['commitId']); return get(`/commits/${digest(input.commitId)}`, 16777216);
      case 'getArtifacts': input = fields(raw, ['runId']); return get(`/runs/${id(input.runId)}/artifacts`);
      case 'getCommitArtifacts': input = fields(raw, ['commitId'], ['afterId', 'limit']); return page(`/commits/${digest(input.commitId)}/artifacts`);
      case 'listExecutions': input = fields(raw, ['runId'], ['afterId', 'limit']); return page(`/runs/${id(input.runId)}/executions`);
      case 'getExecution': input = fields(raw, ['runId', 'callId']); return get(`/runs/${id(input.runId)}/executions/${id(input.callId)}`);
      case 'getExecutionContent': {
        input = fields(raw, ['runId', 'callId', 'resource'], ['offset', 'limit']);
        const resource = text(input.resource, 30);
        if (!RESOURCES.includes(resource)) throw new DesktopError('validation', '执行证据类别不在白名单中');
        return get(query(`/runs/${id(input.runId)}/executions/${id(input.callId)}/content/${resource}`,
          { offset: number(input.offset, 0, 8388608), limit: number(input.limit, 65536, 1048576, 1) }), 1048576);
      }
      case 'getEvents': case 'getApprovals': {
        input = fields(raw, ['runId'], ['afterSeq', 'limit']);
        return get(query(operation === 'getEvents' ? `/runs/${id(input.runId)}/events` : '/approvals',
          { ...(operation === 'getApprovals' ? { run_id: id(input.runId) } : {}),
            after_seq: number(input.afterSeq, 0, Number.MAX_SAFE_INTEGER), limit: number(input.limit, 100, 1000, 1) }));
      }
      case 'listBranches': input = fields(raw, [], ['afterName', 'limit']); return get(query(`/repositories/${this.state.repositoryId ?? ''}/branches`,
        { after_name: input.afterName === undefined ? undefined : text(input.afterName, 100), limit: number(input.limit, 100, 1000, 1) }));
      case 'listCommits': input = fields(raw, [], ['branchName', 'afterCommit', 'limit']); return get(query(`/repositories/${this.state.repositoryId ?? ''}/commits`,
        { branch_name: input.branchName === undefined ? 'main' : text(input.branchName, 100), after_commit: input.afterCommit === undefined ? undefined : digest(input.afterCommit), limit: number(input.limit, 100, 1000, 1) }));
      case 'getDiff': input = fields(raw, ['base', 'head'], ['after', 'limit']); return get(query('/diff',
        { base: digest(input.base), head: digest(input.head), after: number(input.after, 0, Number.MAX_SAFE_INTEGER), limit: number(input.limit, 100, 1000, 1) }));
      case 'createRepository': input = fields(raw, ['name', 'key']); return post('/repositories', { name: text(input.name), expected_version: 0 });
      case 'createAgent': input = fields(raw, ['config', 'expectedVersion', 'key']); return post('/agents',
        { config: jsonObject(input.config), expected_version: number(input.expectedVersion, 0, Number.MAX_SAFE_INTEGER) });
      case 'createTask': input = fields(raw, ['configId', 'configVersion', 'goal', 'key']); return post('/tasks',
        { config_id: id(input.configId), config_version: number(input.configVersion, 1, Number.MAX_SAFE_INTEGER, 1), goal: jsonObject(input.goal), expected_version: 0 });
      case 'submitRun': input = fields(raw, ['taskId', 'configVersion', 'expectedBranchVersion', 'expectedHead', 'branchName', 'key'], ['budget']);
        return post(`/tasks/${id(input.taskId)}/runs`, { config_version: number(input.configVersion, 1, Number.MAX_SAFE_INTEGER, 1),
          expected_branch_version: number(input.expectedBranchVersion, 0, Number.MAX_SAFE_INTEGER), expected_head: input.expectedHead === null ? null : digest(input.expectedHead),
          branch_name: text(input.branchName, 100), ...(input.budget === undefined ? {} : { budget: jsonObject(input.budget) }) });
      case 'createBranch': input = fields(raw, ['sourceRunId', 'fromCommit', 'branchName', 'key']); return post('/branches',
        { source_run_id: id(input.sourceRunId), from_commit: digest(input.fromCommit), name: text(input.branchName, 100), expected_version: 0 });
      case 'replay': input = fields(raw, ['request', 'key']); return post('/replays', jsonObject(input.request));
      case 'controlRun': {
        input = fields(raw, ['runId', 'action', 'expectedVersion', 'key']);
        const action = text(input.action, 10);
        if (!['pause', 'resume', 'cancel'].includes(action)) throw new DesktopError('validation', '不支持的运行控制');
        return post(`/runs/${id(input.runId)}/${action}`, { expected_version: number(input.expectedVersion, 0, Number.MAX_SAFE_INTEGER) });
      }
      case 'decideApproval': {
        input = fields(raw, ['approvalId', 'decision', 'expectedVersion', 'key']);
        const decision = text(input.decision, 10);
        if (!['approve', 'reject'].includes(decision)) throw new DesktopError('validation', '不支持的审批决定');
        return post(`/approvals/${id(input.approvalId)}/decision`, { decision, expected_version: number(input.expectedVersion, 0, Number.MAX_SAFE_INTEGER) });
      }
      default: throw new DesktopError('validation', '业务操作不在白名单中');
    }
  }
}
