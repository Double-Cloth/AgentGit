import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { BusinessController, jsonObject, type Preferences, type SaveArtifact } from '../src/main/routes';
import { DesktopError } from '../src/main/api-client';
import type { BinaryChunk, ConnectionState } from '../src/shared/bridge';

const TOKEN = 'fixture-desktop-token-00000000000000000000';
const REPO = '00000000-0000-4000-8000-000000000001';
const OTHER = '00000000-0000-4000-8000-000000000002';
const FEATURES = ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff'];
const rejected = (code: string) => (error: unknown) => error instanceof DesktopError && error.code === code;
async function fixture(options: {
  token?: string | null; version?: string; ready?: boolean;
  respond?: (request: IncomingMessage, response: ServerResponse) => boolean;
  persist?: (preferences: Preferences) => Promise<void>; save?: SaveArtifact;
} = {}) {
  const requests: IncomingMessage[] = [];
  const server = createServer((request, response) => {
    requests.push(request); response.setHeader('Content-Type', 'application/json');
    if (options.respond?.(request, response)) return;
    const path = new URL(request.url!, 'http://localhost').pathname;
    if (path === '/health') response.end(JSON.stringify({ status: 'ok', version: options.version ?? '0.1.0' }));
    else if (path === '/ready') response.end(JSON.stringify({ status: options.ready === false ? 'unavailable' : 'ready' }));
    else if (path === '/capabilities') response.end(JSON.stringify({ schema_version: 1, version: options.version ?? '0.1.0', features: FEATURES }));
    else if (/^\/repositories\/[0-9a-f-]+$/.test(path)) response.end(JSON.stringify({ id: path.split('/').at(-1) }));
    else response.end(JSON.stringify({ items: [], id: REPO }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const address = server.address(); assert.ok(address && typeof address !== 'string');
  const baseUrl = `http://127.0.0.1:${address.port}`;
  const controller = new BusinessController({ baseUrl, repositoryId: null },
    options.token === null ? undefined : options.token ?? TOKEN,
    options.persist ?? (async () => {}), options.save ?? (async () => ({ cancelled: true })));
  return { controller, requests, baseUrl, close: () => new Promise<void>(resolve => {
    controller.cancelReads(); server.closeAllConnections(); server.close(() => resolve());
  }) };
}
async function connect(controller: BusinessController) {
  await controller.handle('getConnection', undefined);
  await controller.handle('selectRepository', { repositoryId: REPO });
}

test('连接状态区分数据库、身份、兼容性，不能从存活推断 Worker 或 Docker', async () => {
  for (const options of [{}, { token: null }, { version: '0.2.0' }, { ready: false }]) {
    const f = await fixture(options);
    try {
      const state = await f.controller.handle('getConnection', undefined) as ConnectionState;
      assert.equal(state.api, 'reachable'); assert.equal(state.worker, 'unknown'); assert.equal(state.sandbox, 'unknown');
      assert.equal(state.identity, 'token' in options ? 'missing' : 'version' in options ? 'unknown' : 'accepted');
      assert.equal(state.database, 'ready' in options ? 'unavailable' : 'ready');
      if ('version' in options) assert.equal(state.compatibility, 'incompatible');
      if ('token' in options || 'version' in options) assert.ok(!f.requests.some(r => r.url === '/capabilities'));
      for (const r of f.requests.filter(r => ['/health', '/ready'].includes(r.url!))) assert.equal(r.headers.authorization, undefined);
    } finally { await f.close(); }
  }
});

test('业务白名单、参数和项目归属在请求前验证，POST 保持原幂等键且只发一次', async () => {
  const f = await fixture();
  try {
    await connect(f.controller);
    const before = f.requests.length;
    for (const [operation, args] of [
      ['fetch', { url: 'http://localhost:9999' }], ['listRuns', { repositoryId: OTHER }],
      ['listRuns', { path: '/private', headers: {} }], ['getRun', { runId: '../secret' }],
      ['listRuns', { limit: true }], ['listRuns', { limit: 0 }],
      ['getExecutionContent', { runId: REPO, callId: OTHER, resource: 'raw_cas' }],
      ['controlRun', { runId: REPO, action: ['pause'], expectedVersion: 1, key: 'one' }],
      ['decideApproval', { approvalId: REPO, decision: ['approve'], expectedVersion: 1, key: 'one' }],
      ['exportArtifact', { artifactId: REPO, path: 'C:\\private.txt' }],
    ]) await assert.rejects(f.controller.handle(operation, args), rejected('validation'));
    assert.equal(f.requests.length, before);
    await f.controller.handle('listRuns', { limit: 1 });
    const read = f.requests.at(-1)!;
    assert.equal(read.url, `/repositories/${REPO}/runs?limit=1`);
    assert.equal(read.headers['x-repository-id'], REPO);
    assert.equal(read.headers.origin, undefined);
    await f.controller.handle('getDiff', { base: 'a'.repeat(64), head: 'b'.repeat(64) });
    assert.equal(f.requests.at(-1)!.url, `/diff?left_commit=${'a'.repeat(64)}&right_commit=${'b'.repeat(64)}&after=0&limit=100`);
    await f.controller.handle('controlRun', { runId: REPO, action: 'pause', expectedVersion: 2, key: 'same-action' });
    const posts = f.requests.filter(r => r.method === 'POST');
    assert.equal(posts.length, 1); assert.equal(posts[0]!.headers['idempotency-key'], 'same-action');
    assert.equal(posts[0]!.headers['x-repository-id'], REPO);
  } finally { await f.close(); }
});

test('切换项目取消旧读取，随后只能使用已选择项目', async () => {
  let received: (() => void) | undefined;
  const waiting = new Promise<void>(resolve => { received = resolve; });
  const f = await fixture({ respond: (request) => {
    if (request.url?.includes('/runs?')) { received!(); return true; } return false;
  } });
  try {
    await connect(f.controller);
    const old = f.controller.handle('listRuns', {});
    const done = assert.rejects(old, rejected('request_cancelled'));
    await waiting;
    const state = await f.controller.handle('selectRepository', { repositoryId: OTHER }) as ConnectionState;
    await done; assert.equal(state.repositoryId, OTHER);
    await f.controller.handle('listBranches', {});
    assert.equal(f.requests.at(-1)!.headers['x-repository-id'], OTHER);
  } finally { await f.close(); }
});

test('较晚完成的旧连接探测不能覆盖新端点与能力', async () => {
  let release: (() => void) | undefined, started: (() => void) | undefined;
  const waiting = new Promise<void>(resolve => { started = resolve; });
  const old = await fixture({ respond: (request, response) => {
    if (request.url === '/capabilities') {
      release = () => response.end(JSON.stringify({ schema_version: 1, version: '0.1.0', features: FEATURES })); started!(); return true;
    } return false;
  } });
  const fresh = await fixture({ version: '0.2.0' });
  try {
    const pending = old.controller.handle('getConnection', undefined);
    const stale = assert.rejects(pending, rejected('context_changed'));
    await waiting;
    const state = await old.controller.handle('configureConnection', { baseUrl: fresh.baseUrl }) as ConnectionState;
    release!(); await stale;
    assert.equal(state.baseUrl, fresh.baseUrl); assert.equal(state.compatibility, 'incompatible');
    await assert.rejects(old.controller.handle('capabilities', undefined), rejected('connection_required'));
  } finally { await Promise.all([old.close(), fresh.close()]); }
});

test('历史产物验证类型化摘要，保留原字节且只能经主进程保存回调导出', async () => {
  const bytes = Buffer.from([0, 255, 10, 34, 42]);
  const digest = createHash('sha256').update(`agentgit\0AGJSON-v1\0blob\0${bytes.length}\0`).update(bytes).digest('hex');
  const saved: { name: string; bytes: Buffer }[] = [];
  let corrupt = false;
  const f = await fixture({
    respond: (request, response) => {
      if (!request.url?.startsWith('/artifacts/')) return false;
      response.setHeader('ETag', `"${corrupt ? '0'.repeat(64) : digest}"`);
      response.setHeader('Content-Disposition', "attachment; filename*=UTF-8''..%2FCON%3A.txt"); response.end(bytes); return true;
    },
    save: async (name, content) => { saved.push({ name, bytes: content }); return { cancelled: false, name }; },
  });
  try {
    await connect(f.controller);
    const chunk = await f.controller.handle('readArtifact', { artifactId: REPO, offset: 1, limit: 2 }) as BinaryChunk;
    assert.deepEqual(Buffer.from(chunk.base64, 'base64'), bytes.subarray(1, 3));
    assert.equal(chunk.nextOffset, 3); assert.equal(chunk.totalBytes, 5);
    await f.controller.handle('exportArtifact', { artifactId: REPO });
    assert.equal(saved.length, 1); assert.deepEqual(saved[0]!.bytes, bytes);
    assert.ok(!/[\\/:]/.test(saved[0]!.name));
    corrupt = true;
    await assert.rejects(f.controller.handle('exportArtifact', { artifactId: REPO }), rejected('object_corrupt'));
    assert.equal(saved.length, 1);
  } finally { await f.close(); }
});

test('JSON 业务载荷拒绝非 JSON、无限深度、浮点与无效 Unicode', () => {
  const cyclic: Record<string, unknown> = {}; cyclic.self = cyclic;
  for (const value of [[], new Date(), { x: NaN }, { x: 1.5 }, { x: undefined }, { x: '\ud800' }, cyclic]) {
    assert.throws(() => jsonObject(value), DesktopError);
  }
  let deep: Record<string, unknown> = {}; for (let i = 0; i < 70; i++) deep = { next: deep };
  assert.throws(() => jsonObject(deep), rejected('output_limit'));
  assert.deepEqual(jsonObject({ text: '中文😀', count: 5, flag: false, empty: null }), { text: '中文😀', count: 5, flag: false, empty: null });
});

test('切页取消业务读取不能把并行连接探测误报为断连', async () => {
  let delayed = false, release: (() => void) | undefined, entered: (() => void) | undefined;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const f = await fixture({ respond: (request, response) => {
    if (delayed && request.url === '/health') {
      entered!(); release = () => response.end(JSON.stringify({ status: 'ok', version: '0.1.0' })); return true;
    } return false;
  } });
  try {
    await connect(f.controller); delayed = true;
    const probe = f.controller.handle('getConnection', undefined);
    await started; f.controller.cancelReads(); release!();
    const state = await probe as ConnectionState;
    assert.equal(state.api, 'reachable'); assert.equal(state.identity, 'accepted'); assert.equal(state.repositoryId, REPO);
  } finally { await f.close(); }
});
