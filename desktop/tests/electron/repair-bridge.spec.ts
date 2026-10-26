import { test, expect } from '@playwright/test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const REPO = '00000000-0000-4000-8000-000000000001', SEARCH = '00000000-0000-4000-8000-000000000002';
const TOKEN = 'fixture-repair-bridge-token-00000000000000000000';

test('S10 真实 Electron preload：固定修复接口、原键、隔离和非法票据拒绝', async () => {
  const posts: { path: string; key: string | undefined; repository: string | undefined; body: unknown }[] = [];
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json');
    if (request.method === 'POST') {
      const chunks: Buffer[] = []; request.on('data', chunk => chunks.push(chunk)); request.on('end', () => {
        posts.push({ path: request.url!, key: request.headers['idempotency-key'] as string | undefined, repository: request.headers['x-repository-id'] as string | undefined, body: JSON.parse(Buffer.concat(chunks).toString()) });
        response.end(JSON.stringify({ search_id: SEARCH, manifest_ref: 'a'.repeat(64), validation_suite: { suite_id: REPO } }));
      }); return;
    }
    const path = request.url!.split('?')[0];
    response.end(JSON.stringify(path === '/health' ? { status: 'ok', version: '0.1.0' } : path === '/ready' ? { status: 'ready' }
      : path === '/capabilities' ? { schema_version: 1, version: '0.1.0', features: ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff', 'prepared_repair_search'] }
      : path === `/repositories/${REPO}` ? { id: REPO } : { items: [] }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const address = server.address(); if (!address || typeof address === 'string') throw new Error('测试端点不可用');
  const folder = await mkdtemp(join(tmpdir(), 'agentgit-s10-bridge-'));
  const desktop = await launchDesktop(join(folder, 'profile'), `http://127.0.0.1:${address.port}`, TOKEN);
  const errors: string[] = [];
  try {
    const page = await desktop.firstWindow(); page.on('pageerror', error => errors.push(error.message));
    const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(REPO); await page.getByRole('button', { name: '选择项目', exact: true }).click();
    await expect(page.getByRole('heading', { name: '运行', exact: true })).toBeVisible();
    const result = await page.evaluate(async ({ repo, search }) => {
      const api = window.agentgit;
      const operations = ['createPreparedRepairSearch', 'getRepairSearch', 'listRepairSearches', 'cancelRepairSearch', 'createRepairStudy', 'getRepairStudy', 'cancelRepairStudy', 'getRepairValidationSuite', 'openRepairValidation', 'getRepairValidation', 'createRepairProposal', 'getRepairProposal', 'beginRepairReview', 'decideRepairReview'];
      const exposed = operations.every(name => typeof (api as unknown as Record<string, unknown>)[name] === 'function');
      const created = await api.createPreparedRepairSearch({ request: { search: {}, regression: [] }, key: 'native-prepared' });
      const read = await api.getRepairSearch({ searchId: search });
      const ticket = { proposalId: search, reviewId: repo, proposalRef: 'a'.repeat(64), expectedConfigVersion: 29, key: 'native-decision' };
      const bad = await api.decideRepairReview({ ...ticket, decision: 'approve', expectedConfigVersion: 0 });
      const saved = await api.decideRepairReview({ ...ticket, decision: 'save_experiment' });
      return { exposed, created, read, bad, saved, requireType: typeof (window as unknown as { require?: unknown }).require, processType: typeof (window as unknown as { process?: unknown }).process };
    }, { repo: REPO, search: SEARCH });
    expect(result.exposed).toBe(true); expect(result.created.ok).toBe(true); expect(result.read.ok).toBe(true); expect(result.saved.ok).toBe(true);
    expect(result.bad.ok).toBe(false); if (!result.bad.ok) expect(result.bad.error.code).toBe('validation');
    expect(result.requireType).toBe('undefined'); expect(result.processType).toBe('undefined');
    expect(JSON.stringify(result)).not.toContain(TOKEN); expect(posts).toHaveLength(2);
    expect(posts[0]!.path).toBe('/repair-searches/prepared'); expect(posts[0]!.key).toBe('native-prepared');
    expect(posts[1]).toEqual({ path: `/repair-proposals/${SEARCH}/approve`, key: 'native-decision', repository: REPO, body: { schema_version: 1, review_id: REPO, proposal_ref: 'a'.repeat(64), expected_config_version: 29, decision: 'save_experiment' } });
    expect(errors).toEqual([]);
    await writeFile(join(folder, 'bridge.json'), JSON.stringify({ runtime, posts, result, errors }, null, 2)); console.log(`S10 桥接证据：${folder}`);
  } finally { try { await desktop.close(); } finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); } }
});
