import { request as httpRequest, type IncomingHttpHeaders } from 'node:http';
import type { Json, JsonObject } from '../shared/bridge';

export class DesktopError extends Error {
  constructor(readonly code: string, message: string) { super(message); }
}

export function normalizeBaseUrl(value: unknown): string {
  if (typeof value !== 'string' || value.length > 300) throw new DesktopError('validation', 'API 地址无效');
  let url: URL;
  try { url = new URL(value); } catch { throw new DesktopError('validation', 'API 地址无效'); }
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)
      || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new DesktopError('validation', '仅支持明确配置的回环 HTTP 根地址');
  }
  url.hostname = '127.0.0.1';
  return url.origin;
}

export interface ApiRequest {
  method: 'GET' | 'POST';
  path: string;
  repository?: string;
  key?: string;
  body?: JsonObject;
  maxBytes?: number;
  publicProbe?: boolean;
}
export interface BytesResponse { body: Buffer; headers: IncomingHttpHeaders; status: number }

export class ApiClient {
  readonly baseUrl: string;
  private readonly pendingReads = new Map<ReturnType<typeof httpRequest>, () => void>();
  private active = 0;
  constructor(baseUrl: string, private readonly token: string | undefined, private readonly timeoutMs = 10000) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
  }
  get tokenConfigured(): boolean { return Boolean(this.token); }
  cancelReads(): void {
    for (const cancel of this.pendingReads.values()) cancel();
    this.pendingReads.clear();
  }
  private redact(value: Json): Json {
    if (typeof value === 'string') return this.token ? value.replaceAll(this.token, '[已隐藏]') : value;
    if (Array.isArray(value)) return value.map(item => this.redact(item));
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [
        this.token ? key.replaceAll(this.token, '[已隐藏]') : key, this.redact(item),
      ]));
    }
    return value;
  }
  async json(spec: ApiRequest): Promise<JsonObject> {
    const response = await this.bytes(spec);
    let parsed: Json;
    try { parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(response.body)) as Json; }
    catch { throw new DesktopError('invalid_response', 'API 未返回有效 JSON；可查询已提交任务，不能假定修改未发生'); }
    if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
      throw new DesktopError('invalid_response', 'API 响应结构无效');
    }
    return this.redact(parsed) as JsonObject;
  }
  assertSafeForIpc(bytes: Buffer): void {
    if (this.token && bytes.includes(Buffer.from(this.token))) {
      throw new DesktopError('credential_in_content', '证据包含连接凭据，已拒绝通过界面传递');
    }
  }
  bytes(spec: ApiRequest): Promise<BytesResponse> {
    if (!spec.publicProbe && !this.token) return Promise.reject(new DesktopError('identity_missing', '请在启动桌面前设置 AGENTGIT_API_TOKEN'));
    if (!spec.publicProbe && this.token && !/^[\x21-\x7e]{32,8192}$/.test(this.token)) {
      return Promise.reject(new DesktopError('identity_invalid', '本地身份令牌格式无效'));
    }
    if (spec.publicProbe && (spec.method !== 'GET' || !['/health', '/ready'].includes(spec.path))) {
      return Promise.reject(new DesktopError('validation', '公开探测只能访问存活和数据库就绪入口'));
    }
    if (!spec.path.startsWith('/') || spec.path.startsWith('//') || /[\r\n\\]/.test(spec.path)) {
      return Promise.reject(new DesktopError('validation', '内部业务路由无效'));
    }
    const url = new URL(spec.path, this.baseUrl);
    if (url.origin !== this.baseUrl) return Promise.reject(new DesktopError('validation', '业务请求不能改变 API 地址'));
    const body = spec.body ? Buffer.from(JSON.stringify(spec.body)) : undefined;
    if ((body?.length ?? 0) > 1048576) return Promise.reject(new DesktopError('output_limit', '请求超过 1 MiB 上限'));
    if (this.active >= 16) return Promise.reject(new DesktopError('busy', '同时读取过多，请稍后重试'));
    const headers: Record<string, string> = {};
    if (this.token && !spec.publicProbe) headers.Authorization = `Bearer ${this.token}`;
    if (spec.repository) headers['X-Repository-ID'] = spec.repository;
    if (spec.key) headers['Idempotency-Key'] = spec.key;
    if (body) { headers['Content-Type'] = 'application/json'; headers['Content-Length'] = String(body.length); }
    const maxBytes = spec.maxBytes ?? 2097152;
    this.active += 1;
    return new Promise((resolve, reject) => {
      let finished = false;
      const finish = (error?: DesktopError, value?: BytesResponse) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        this.pendingReads.delete(req);
        this.active -= 1;
        if (error) reject(error); else if (value) resolve(value);
      };
      const req = httpRequest(url, { method: spec.method, headers }, response => {
        const status = response.statusCode ?? 0;
        if (status >= 300 && status < 400) {
          response.resume();
          req.destroy();
          finish(new DesktopError('redirect_forbidden', 'API 重定向已拒绝'));
          return;
        }
        let size = 0;
        const chunks: Buffer[] = [];
        response.on('data', (chunk: Buffer) => {
          size += chunk.length;
          if (size > maxBytes || chunks.length >= 65536) {
            req.destroy();
            finish(new DesktopError('output_limit', 'API 响应超过该业务的读取上限'));
          } else chunks.push(chunk);
        });
        response.on('end', () => {
          if (finished) return;
          const bytes = Buffer.concat(chunks);
          if (status < 200 || status >= 300) {
            let code = `http_${status}`, message = 'API 请求失败，请查看连接状态或关联任务';
            try {
              const value = JSON.parse(bytes.toString('utf8')) as { error?: { code?: unknown; message?: unknown } };
              if (typeof value.error?.code === 'string' && /^[a-z_]{1,80}$/.test(value.error.code)
                  && !(this.token && value.error.code.includes(this.token))) code = value.error.code;
              if (typeof value.error?.message === 'string') message = String(this.redact(value.error.message)).slice(0, 500);
            } catch { /* 非结构化错误不透传正文。 */ }
            finish(new DesktopError(code, message));
          } else finish(undefined, { body: bytes, headers: response.headers, status });
        });
        const interrupted = () => finish(new DesktopError(spec.method === 'POST' ? 'outcome_unknown' : 'connection_lost', 'API 响应中断，请重新查询任务状态'));
        response.on('error', interrupted);
        response.on('aborted', interrupted);
      });
      const timer = setTimeout(() => {
        req.destroy();
        finish(new DesktopError(spec.method === 'POST' ? 'outcome_unknown' : 'connection_timeout',
          spec.method === 'POST' ? '提交结果尚未确认；保留原幂等键并查询，不要创建重复动作' : 'API 读取超时'));
      }, this.timeoutMs);
      req.on('error', error => finish(error instanceof DesktopError ? error : new DesktopError(
        spec.method === 'POST' ? 'outcome_unknown' : 'connection_unavailable',
        spec.method === 'POST' ? '提交结果尚未确认；保留原幂等键并查询' : '无法连接本地 API，请检查服务地址与启动状态',
      )));
      if (spec.method === 'GET') this.pendingReads.set(req, () => {
        finish(new DesktopError('request_cancelled', '读取已取消'));
        req.destroy();
      });
      req.end(body);
    });
  }
}
