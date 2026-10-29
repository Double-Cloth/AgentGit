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

test('S10 桥接固定搜索、缩减、复测及精确审阅路由，不提供任意 URL 或自动采纳', async () => {
  const posts: { path: string; body: unknown; key: string | undefined; repository: string | undefined }[] = [];
  const f = await fixture({ respond: (request, response) => {
    if (request.method !== 'POST') return false;
    const chunks: Buffer[] = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => {
      posts.push({ path: request.url!, body: JSON.parse(Buffer.concat(chunks).toString()), key: request.headers['idempotency-key'] as string | undefined, repository: request.headers['x-repository-id'] as string | undefined });
      response.end(JSON.stringify({ accepted: true, expected_config_version: 29 }));
    }); return true;
  } });
  try {
    await connect(f.controller);
    for (const [operation, input, path] of [
      ['getRepairSearch', { searchId: OTHER }, `/repair-searches/${OTHER}`],
      ['listRepairSearches', { afterId: OTHER, limit: 100 }, `/repositories/${REPO}/repair-searches?after_id=${OTHER}&limit=100`],
      ['getRepairStudy', { studyId: OTHER }, `/repair-studies/${OTHER}`],
      ['getRepairValidationSuite', { suiteId: OTHER }, `/repair-validation-suites/${OTHER}`],
      ['getRepairValidation', { studyId: OTHER }, `/repair-studies/${OTHER}/validation`],
      ['getRepairProposal', { proposalId: OTHER }, `/repair-proposals/${OTHER}`],
    ] as const) {
      await f.controller.handle(operation, input); assert.equal(f.requests.at(-1)!.url, path);
    }
    const prepared = { search: { schema_version: 1 }, regression: [] };
    await f.controller.handle('createPreparedRepairSearch', { request: prepared, key: 'prepared' });
    await f.controller.handle('createRepairStudy', { request: { search_id: OTHER }, key: 'study' });
    await f.controller.handle('cancelRepairSearch', { searchId: OTHER, key: 'cancel-search' });
    await f.controller.handle('cancelRepairStudy', { studyId: OTHER, key: 'cancel-study' });
    await f.controller.handle('openRepairValidation', { studyId: OTHER, suiteId: REPO, key: 'open' });
    await f.controller.handle('createRepairProposal', { studyId: OTHER, key: 'proposal' });
    await f.controller.handle('beginRepairReview', { proposalId: OTHER, key: 'review' });
    for (const decision of ['approve', 'reject', 'save_experiment']) {
      await f.controller.handle('decideRepairReview', { proposalId: OTHER, reviewId: REPO, proposalRef: 'a'.repeat(64), expectedConfigVersion: 29, decision, key: decision });
    }
    assert.equal(posts.length, 10);
    assert.deepEqual(posts[0], { path: '/repair-searches/prepared', body: { schema_version: 1, ...prepared }, key: 'prepared', repository: REPO });
    assert.deepEqual(posts[4]!.body, { schema_version: 1, suite_id: REPO });
    assert.deepEqual(posts.slice(7).map(row => row.path), [`/repair-proposals/${OTHER}/approve`, `/repair-proposals/${OTHER}/reject`, `/repair-proposals/${OTHER}/approve`]);
    assert.deepEqual(posts[9]!.body, { schema_version: 1, review_id: REPO, proposal_ref: 'a'.repeat(64), expected_config_version: 29, decision: 'save_experiment' });
    assert.ok(posts.every(row => row.repository === REPO && row.key));
  } finally { await f.close(); }
});

test('S10 修复决定拒绝不精确票据、越界版本、路径和附加执行字段，拒绝发生在 HTTP 前', async () => {
  const f = await fixture();
  try {
    await connect(f.controller);
    const before = f.requests.length;
    const good = { proposalId: OTHER, reviewId: REPO, proposalRef: 'a'.repeat(64), expectedConfigVersion: 29, decision: 'approve', key: 'exact' };
    for (const change of [{ reviewId: '../private' }, { proposalRef: 'A'.repeat(64) }, { expectedConfigVersion: 0 }, { expectedConfigVersion: true }, { expectedConfigVersion: undefined }, { expectedConfigVersion: Number.MAX_SAFE_INTEGER + 1 }, { decision: 'apply' }, { execute: true }, { url: 'http://evil.invalid' }]) {
      await assert.rejects(f.controller.handle('decideRepairReview', { ...good, ...change }), rejected('validation'));
    }
    await assert.rejects(f.controller.handle('getRepairSearch', { searchId: '../private' }), rejected('validation'));
    await assert.rejects(f.controller.handle('listRepairSearches', { limit: 101 }), rejected('validation'));
    await assert.rejects(f.controller.handle('createPreparedRepairSearch', { request: {}, key: 'k', repositoryId: OTHER }), rejected('validation'));
    assert.equal(f.requests.length, before);
  } finally { await f.close(); }
});
async function fixture(options: {
  token?: string | null; version?: string; ready?: boolean;
  features?: string[];
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
    else if (path === '/capabilities') response.end(JSON.stringify({ schema_version: 1, version: options.version ?? '0.1.0', features: options.features ?? FEATURES }));
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

test('S8 固定报告路由绑定当前项目，候选 ID 与精确运行参数分别验证', async () => {
  const f = await fixture();
  try {
    await connect(f.controller);
    await f.controller.handle('getDebugReport', { reportId: OTHER });
    assert.equal(f.requests.at(-1)!.url, `/debug-reports/${OTHER}`);
    await f.controller.handle('listDebugReports', { afterId: OTHER, limit: 10 });
    assert.equal(f.requests.at(-1)!.url, `/repositories/${REPO}/debug-reports?after_id=${OTHER}&limit=10`);
    await f.controller.handle('getDebugCandidateContext', { reportId: OTHER, candidateEventId: 9001, validatorRef: 'b'.repeat(64) });
    assert.equal(f.requests.at(-1)!.url, `/debug-reports/${OTHER}/candidate-context?candidate_event_id=9001&validator_ref=${'b'.repeat(64)}`);
    await f.controller.handle('getDebugCandidateExperiment', { reportId: OTHER, candidateEventId: 9001, experimentId: REPO });
    assert.equal(f.requests.at(-1)!.url, `/debug-reports/${OTHER}/candidate-experiment?candidate_event_id=9001&experiment_id=${REPO}`);
    await f.controller.handle('getBehavioralDiff', { leftRunId: REPO, rightRunId: OTHER });
    assert.equal(f.requests.at(-1)!.url, `/behavioral-diff?left_run_id=${REPO}&right_run_id=${OTHER}`);
    await f.controller.handle('createDebugReport', { failedRunId: OTHER, key: 'same-report' });
    assert.equal(f.requests.at(-1)!.method, 'POST');
    assert.equal(f.requests.at(-1)!.headers['idempotency-key'], 'same-report');
    assert.equal(f.requests.at(-1)!.headers['x-repository-id'], REPO);
  } finally { await f.close(); }
});
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
      ['getRepository', { repositoryId: OTHER }], ['getRepository', { path: '/private' }],
      ['listRuns', { path: '/private', headers: {} }], ['getRun', { runId: '../secret' }],
      ['listRuns', { limit: true }], ['listRuns', { limit: 0 }],
      ['getExecutionContent', { runId: REPO, callId: OTHER, resource: 'raw_cas' }],
      ['controlRun', { runId: REPO, action: ['pause'], expectedVersion: 1, key: 'one' }],
      ['decideApproval', { approvalId: REPO, decision: ['approve'], expectedVersion: 1, key: 'one' }],
      ['exportArtifact', { artifactId: REPO, path: 'C:\\private.txt' }],
      ['getExperiment', { experimentId: '../private' }], ['getValidator', { validatorRef: REPO }],
      ['listExperiments', { repositoryId: OTHER }], ['listExperiments', { limit: 101 }],
      ...[0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, OTHER, undefined].map(targetEventId => ['getInterventionContext', { sourceRunId: REPO, fromCommit: 'a'.repeat(64), targetEventId, validatorRef: 'b'.repeat(64) }]),
      ['getIntervention', { interventionId: OTHER, repositoryId: OTHER }],
      ['getInterventionContext', { sourceRunId: REPO, fromCommit: 'a'.repeat(64), targetEventId: OTHER, validatorRef: 'b'.repeat(64), url: 'http://localhost:9999' }],
      ['getDebugReport', { reportId: '../private' }], ['listDebugReports', { repositoryId: OTHER }],
      ['createDebugReport', { failedRunId: REPO, referenceRunId: '../private', key: 'report' }],
      ['getBehavioralDiff', { leftRunId: REPO, rightRunId: '../private' }],
      ...[0, -1, true, '2', undefined, Number.MAX_SAFE_INTEGER + 1].map(candidateEventId => ['getDebugCandidateContext', { reportId: OTHER, candidateEventId, validatorRef: 'b'.repeat(64) }]),
      ['getDebugCandidateExperiment', { reportId: OTHER, candidateEventId: 2, experimentId: REPO, path: '/private' }],
      ['freezeValidator', { sourceRunId: REPO, fromCommit: 'a'.repeat(64), fixtureSplit: 'everything', key: 'freeze' }],
      ['createExperiment', { request: [], key: 'experiment' }],
      ['createIntervention', { request: {}, key: 'patch', path: '/private' }],
    ]) await assert.rejects(f.controller.handle(operation, args), rejected('validation'));
    assert.equal(f.requests.length, before);
    await f.controller.handle('listRuns', { limit: 1 });
    const read = f.requests.at(-1)!;
    assert.equal(read.url, `/repositories/${REPO}/runs?limit=1`);
    assert.equal(read.headers['x-repository-id'], REPO);
    assert.equal(read.headers.origin, undefined);
    await f.controller.handle('listExperiments', { afterId: OTHER, limit: 2 });
    assert.equal(f.requests.at(-1)!.url, `/repositories/${REPO}/experiments?after_id=${OTHER}&limit=2`);
    await f.controller.handle('getRepository', undefined);
    assert.equal(f.requests.at(-1)!.url, `/repositories/${REPO}`);
    assert.equal(f.requests.at(-1)!.headers['x-repository-id'], REPO);
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
    await f.controller.handle('getRepository', undefined);
    assert.equal(f.requests.at(-1)!.url, `/repositories/${OTHER}`);
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

test('S7 只桥接固定实验端点，绑定项目和幂等键并保留精确统计字符串', async () => {
  const bodies: Record<string, unknown>[] = [];
  const f = await fixture({ respond: (request, response) => {
    if (request.url === `/experiments/${REPO}`) {
      response.end(JSON.stringify({ statistics: { mean: { numerator: '9007199254740993', denominator: '2' } } })); return true;
    }
    if (request.method === 'POST' && ['/validators', '/experiments', '/interventions'].includes(request.url!)) {
      const chunks: Buffer[] = [];
      request.on('data', chunk => chunks.push(chunk));
      request.on('end', () => { bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>); response.end(JSON.stringify({ accepted: true })); });
      return true;
    }
    return false;
  } });
  try {
    await connect(f.controller);
    const before = f.requests.length;
    await f.controller.handle('freezeValidator', { sourceRunId: REPO, fromCommit: 'a'.repeat(64), fixtureSplit: 'confirm', key: 'frozen-validator' });
    await f.controller.handle('getValidator', { validatorRef: 'b'.repeat(64) });
    await f.controller.handle('createExperiment', { request: { repeats: 5 }, key: 'experiment-key' });
    const result = await f.controller.handle('getExperiment', { experimentId: REPO }) as { statistics: { mean: { numerator: string; denominator: string } } };
    assert.deepEqual(result.statistics.mean, { numerator: '9007199254740993', denominator: '2' });
    await f.controller.handle('getInterventionContext', { sourceRunId: REPO, fromCommit: 'a'.repeat(64), targetEventId: 17, validatorRef: 'b'.repeat(64) });
    await f.controller.handle('createIntervention', { request: { operator: 'PROMPT_PATCH' }, key: 'intervention-key' });
    await f.controller.handle('getIntervention', { interventionId: OTHER });
    const requests = f.requests.slice(before);
    assert.deepEqual(requests.map(request => new URL(request.url!, 'http://localhost').pathname), [
      '/validators', `/validators/${'b'.repeat(64)}`, '/experiments', `/experiments/${REPO}`, '/intervention-context', '/interventions', `/interventions/${OTHER}`,
    ]);
    assert.ok(requests.every(request => request.headers['x-repository-id'] === REPO));
    assert.deepEqual(requests.filter(request => request.method === 'POST').map(request => request.headers['idempotency-key']), ['frozen-validator', 'experiment-key', 'intervention-key']);
    assert.deepEqual(bodies, [
      { schema_version: 1, source_run_id: REPO, from_commit: 'a'.repeat(64), fixture_split: 'confirm' },
      { schema_version: 1, repeats: 5 }, { schema_version: 1, operator: 'PROMPT_PATCH' },
    ]);
    const query = new URL(requests[4]!.url!, 'http://localhost').searchParams;
    assert.equal(query.get('target_event_id'), '17'); assert.equal(query.get('validator_ref'), 'b'.repeat(64));
    assert.equal(query.get('source_run_id'), REPO); assert.equal(query.get('from_commit'), 'a'.repeat(64));
  } finally { await f.close(); }
});
