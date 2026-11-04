import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { BusinessController } from '../src/main/routes';
import { DesktopError } from '../src/main/api-client';

const REPO = '00000000-0000-4000-8000-000000000001', OTHER = '00000000-0000-4000-8000-000000000002';
const A = 'a'.repeat(64), B = 'b'.repeat(64);
const requiredFeatures = ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff'];
async function fixture(merge = true) {
  const calls: { method: string; path: string; body: unknown; key?: string; repository?: string }[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', chunk => chunks.push(chunk)); request.on('end', () => {
      calls.push({ method: request.method!, path: request.url!, body: chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null,
        key: request.headers['idempotency-key'] as string, repository: request.headers['x-repository-id'] as string });
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify(request.url === '/health' ? { status: 'ok', version: '0.1.0' } : request.url === '/ready' ? { status: 'ready' }
        : request.url === '/capabilities' ? { schema_version: 1, version: '0.1.0', features: [...requiredFeatures, ...(merge ? ['semantic_merge', 'merged_run_initialization'] : [])] }
          : { items: [], id: REPO }));
    });
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening'); const address = server.address(); assert.ok(address && typeof address === 'object');
  const controller = new BusinessController({ baseUrl: `http://127.0.0.1:${address.port}`, repositoryId: null }, 'fixture-merge-token-000000000000000000', async () => {}, async () => ({}));
  await controller.handle('getConnection', undefined); await controller.handle('selectRepository', { repositoryId: REPO });
  return { controller, calls, close: () => new Promise<void>(resolve => { controller.cancelReads(); server.closeAllConnections(); server.close(() => resolve()); }) };
}
const version = { proposalId: OTHER, expectedRevision: 2, expectedRevisionRef: A, key: 'exact-key' };
const request = { ours: { name: 'ours', head: A, source_run_id: REPO, version: 4 }, theirs: { name: 'theirs', head: B, source_run_id: OTHER, version: 3 }, validator_ref: A };
test('S11 固定合并路由完整绑定项目、版本、票据和幂等键', async () => {
  const f = await fixture();
  try {
    const start = f.calls.length;
    for (const [operation, input, path] of [
      ['listMergeProposals', { limit: 1, afterId: OTHER }, `/repositories/${REPO}/merge-proposals?after_id=${OTHER}&limit=1`],
      ['getMergeBases', { ours: A, theirs: B, selected: A }, `/merge-bases?ours=${A}&theirs=${B}&selected=${A}`],
      ['getMergeProposal', { proposalId: OTHER, reviewBefore: REPO }, `/merge-proposals/${OTHER}?review_before=${REPO}`],
      ['getMergeReview', { proposalId: OTHER, reviewId: REPO }, `/merge-proposals/${OTHER}/reviews/${REPO}`],
    ] as const) { await f.controller.handle(operation, input); assert.equal(f.calls.at(-1)!.path, path); }
    await f.controller.handle('createMergeProposal', { request, key: 'create' });
    await f.controller.handle('replaceMergeRevision', { ...version, edits: { replace_plan: false, sources: { source_a: 'theirs' }, artifacts: [{ path: 'report.md', text: '' }] } });
    await f.controller.handle('validateMergeRevision', version);
    await f.controller.handle('beginMergeReview', { ...version, validationId: REPO });
    await f.controller.handle('approveMergeProposal', { ...version, reviewId: REPO, ticketRef: B, candidateDigest: A });
    await f.controller.handle('rejectMergeProposal', { ...version, reason: '保留冲突' });
    await f.controller.handle('forkMergedBranch', { approvalId: REPO, commitId: B, branchName: 'merged/new', key: 'branch' });
    await f.controller.handle('submitMergedRun', { branchName: 'merged/new', expectedHead: B, expectedBranchVersion: 0, configId: OTHER, configVersion: 7, validatorRef: A, budget: { max_model_calls: 2 }, key: 'run' });
    const calls = f.calls.slice(start), posts = calls.filter(item => item.method === 'POST');
    assert.equal(calls.length, 12); assert.ok(calls.every(item => item.repository === REPO)); assert.ok(posts.every(item => item.key));
    assert.deepEqual(posts.map(item => item.path), ['/merge-proposals', ...['revisions', 'validate', 'review', 'approve', 'reject'].map(suffix => `/merge-proposals/${OTHER}/${suffix}`), '/merge-branches', '/merge-runs']);
    assert.deepEqual(posts[4]!.body, { schema_version: 1, expected_revision: 2, expected_revision_ref: A, review_id: REPO, ticket_ref: B, candidate_digest: A });
    assert.deepEqual(posts[7]!.body, { schema_version: 1, branch_name: 'merged/new', expected_head: B, expected_branch_version: 0, config_id: OTHER, config_version: 7, validator_ref: A, budget: { max_model_calls: 2 } });
  } finally { await f.close(); }
});

test('S11 不精确、越权字段及非法产物在 HTTP 前拒绝，旧后端能力不误报', async () => {
  const f = await fixture();
  try {
    const start = f.calls.length;
    const approve = { ...version, reviewId: REPO, ticketRef: B, candidateDigest: A };
    for (const change of [{ expectedRevision: 0 }, { expectedRevision: true }, { expectedRevision: undefined }, { expectedRevision: 101 }, { expectedRevisionRef: '../x' }, { ticketRef: 'A'.repeat(64) }, { reviewId: 'not-id' }, { actor: 'other' }, { execute: true }, { url: '/private' }]) {
      await assert.rejects(f.controller.handle('approveMergeProposal', { ...approve, ...change }), DesktopError);
    }
    for (const edits of [{ usage: {} }, { plan: {} }, { artifacts: [{ path: '../report.md', text: 'x' }] }, { artifacts: [{ path: 'report.md', text: 'x' }, { path: 'report.md', text: 'x' }] }, { artifacts: [{ path: 'report.md', operation: 'delete', text: 'x' }] }, { sources: { source_a: 'execute' } }, { artifacts: [{ path: 'report.md', text: '中'.repeat(349526) }] }]) {
      await assert.rejects(f.controller.handle('replaceMergeRevision', { ...version, edits }), DesktopError);
    }
    await assert.rejects(f.controller.handle('createMergeProposal', { request: { ...request, execute: true }, key: 'create' }), DesktopError);
    await assert.rejects(f.controller.handle('createMergeProposal', { request: { ...request, ours: { ...request.ours, version: undefined } }, key: 'create' }), DesktopError);
    assert.equal(f.calls.length, start);
  } finally { await f.close(); }
  const old = await fixture(false);
  try { const start = old.calls.length; await assert.rejects(old.controller.handle('listMergeProposals', {}), (error: unknown) => error instanceof DesktopError && error.code === 'unsupported'); assert.equal(old.calls.length, start); }
  finally { await old.close(); }
});

test('S11 两文件与转义文本受完整一 MiB HTTP 限制，不放宽既有接口', async () => {
  const f = await fixture();
  try {
    const edits = { artifacts: [{ path: 'report.md', text: '\u0001'.repeat(65536) }, { path: 'sources.json', text: 'x'.repeat(262144) }] };
    await f.controller.handle('replaceMergeRevision', { ...version, edits });
    assert.deepEqual((f.calls.at(-1)!.body as { edits: unknown }).edits, edits);
    const count = f.calls.length;
    await assert.rejects(f.controller.handle('replaceMergeRevision', { ...version, edits: { artifacts: [{ path: 'report.md', text: 'x'.repeat(524288) }, { path: 'sources.json', text: 'y'.repeat(524288) }] } }), DesktopError);
    assert.equal(f.calls.length, count);
    await assert.rejects(f.controller.handle('createAgent', { config: { text: 'x'.repeat(1048576) }, expectedVersion: 1, key: 'ordinary' }), DesktopError);
  } finally { await f.close(); }
});
