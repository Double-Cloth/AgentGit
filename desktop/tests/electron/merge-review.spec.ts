import { test, expect } from '@playwright/test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const REPO = '00000000-0000-4000-8000-000000000001', OURS = '10000000-0000-4000-8000-000000000001', THEIRS = '20000000-0000-4000-8000-000000000001';
const PROPOSAL = '30000000-0000-4000-8000-000000000001', REVIEW = '40000000-0000-4000-8000-000000000001', APPROVAL = '50000000-0000-4000-8000-000000000001', VALIDATION = '60000000-0000-4000-8000-000000000001';
const A = 'a'.repeat(64), B = 'b'.repeat(64), C = 'c'.repeat(64), D = 'd'.repeat(64), E = 'e'.repeat(64), F = 'f'.repeat(64), M = '8'.repeat(64), T = '9'.repeat(64);
const features = ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff', 'semantic_merge', 'merged_run_initialization'];

test('Merge Review 原键、完整修订、精确本人票据、冲突、批准后独立任务与重开只读', async () => {
  let created = false, lostCreate = true, lostApprove = true, conflict = true, merged = false, revision = 1, validated = false, stale = false, offline = false, edits: unknown = {}, branched = false, queued = false;
  const posts: { path: string; key?: string; body: Record<string, unknown> }[] = [], images: string[] = [], errors: string[] = [];
  const ours = { name: 'ours', head: A, source_run_id: OURS, version: 4 }, theirs = { name: 'theirs', head: B, source_run_id: THEIRS, version: 5 };
  const revisionRef = () => String(revision).repeat(64);
  const ticket = () => ({ review_id: REVIEW, ticket_ref: T, ticket: { proposal_ref: D, revision_ref: revisionRef(), validation_id: VALIDATION, validation_ref: F, candidate_digest: E, ours, theirs, actor: 'api:fixture', execution: 'not_started' } });
  const approval = () => ({ decision_id: APPROVAL, commit_id: M, decision_ref: T, decision: { decision: 'approve', execution: 'requires_new_run' } });
  const body = Buffer.from('## 证据\n<img src=x onerror="window.bad=1">原始文本');
  const blob = createHash('sha256').update(`agentgit\0AGJSON-v1\0blob\0${body.length}\0`).update(body).digest('hex');
  const current = () => ({ proposal_id: PROPOSAL, proposal_ref: D, manifest: { request: { ours, theirs, validator_ref: F }, origins: [{ kind: 'approved_merge', commit_id: C, approval_id: APPROVAL }, { run_id: OURS, commit_id: A }, { run_id: THEIRS, commit_id: B }] }, status: merged ? 'merged' : stale ? 'stale' : validated ? 'validated' : 'draft', revision: { revision, revision_ref: revisionRef(), candidate_digest: E }, candidate: { state: { plan: null, working_memory: {}, constraints: {} }, candidate_digest: E, edits, artifacts: [{ path: 'report.md', status: 'merged', text: '<script>window.bad=1</script>' }, { path: 'sources.json', status: 'merged', text: '{}' }], conflicts: [] }, validation_id: validated ? VALIDATION : null, validation_ref: validated ? F : null, validation: validated ? { proposal_ref: D, revision_ref: revisionRef(), result: { validated: true, candidate_digest: E, conflicts: [], report_evaluation: { passed: true } } } : null, approval: merged ? approval() : null, rejection: null, reviews: [{ ...ticket(), current: !stale && !merged }], revisions: [], requires_recheck: true });
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json'); const path = new URL(request.url!, 'http://localhost').pathname;
    if (offline && ['/health', '/ready'].includes(path)) { response.writeHead(503); response.end('{}'); return; }
    if (request.method === 'POST') {
      const chunks: Buffer[] = []; request.on('data', chunk => chunks.push(chunk)); request.on('end', () => {
        const value = JSON.parse(Buffer.concat(chunks).toString()) as Record<string, unknown>; posts.push({ path, key: request.headers['idempotency-key'] as string, body: value });
        if (path === '/merge-proposals') { created = true; if (lostCreate) { lostCreate = false; response.writeHead(503); response.end(JSON.stringify({ error: { code: 'fixture_lost', message: 'fixture 已提交但响应丢失' } })); return; } response.end(JSON.stringify({ proposal_id: PROPOSAL })); }
        else if (path.endsWith('/revisions')) { revision++; edits = value.edits; validated = false; response.end(JSON.stringify({ proposal_id: PROPOSAL, revision, revision_ref: revisionRef() })); }
        else if (path.endsWith('/validate')) { validated = true; response.end(JSON.stringify({ validation_id: VALIDATION, validation_ref: F })); }
        else if (path.endsWith('/review')) response.end(JSON.stringify(ticket()));
        else if (path.endsWith('/approve')) {
          if (conflict) { conflict = false; stale = true; response.writeHead(409); response.end(JSON.stringify({ error: { code: 'version_conflict', message: 'fixture 来源版本前进' } })); return; }
          merged = true;
          if (lostApprove) { lostApprove = false; response.writeHead(503); response.end(JSON.stringify({ error: { code: 'fixture_lost', message: 'fixture 决定已提交但响应丢失' } })); return; }
          response.end(JSON.stringify(approval()));
        } else if (path === '/merge-branches') { branched = true; response.end(JSON.stringify({ name: value.name, head_commit_id: M, version: 0, origin_kind: 'merge', approval_id: APPROVAL })); }
        else if (path === '/merge-runs') { queued = true; response.end(JSON.stringify({ task_id: OURS, run_id: THEIRS, job_id: REVIEW, mode: 'live', from_commit: M })); }
        else { response.writeHead(404); response.end('{}'); }
      }); return;
    }
    if (path === `/artifacts/${OURS}/content`) { response.setHeader('Content-Type', 'text/markdown'); response.setHeader('ETag', '"' + blob + '"'); response.end(body); return; }
    const result = path === '/health' ? { status: 'ok', version: '0.1.0' } : path === '/ready' ? { status: 'ready' }
      : path === '/capabilities' ? { schema_version: 1, version: '0.1.0', features }
        : path === `/repositories/${REPO}` ? { id: REPO } : path.endsWith('/merge-proposals') ? { items: created ? [{ proposal_id: PROPOSAL, ours_branch: 'ours', theirs_branch: 'theirs', status: merged ? 'merged' : 'open' }] : [], next_after_id: null }
          : path.endsWith('/branches') ? { items: [{ name: 'ours', head_commit_id: A, version: 4 }, { name: 'theirs', head_commit_id: B, version: 5 }, ...(branched ? [{ name: 'merged/fixture', head_commit_id: M, version: 0, origin_kind: 'merge', merge_approval_id: APPROVAL }] : [])], has_more: false }
            : path === '/merge-bases' ? { status: 'ambiguous_merge_base', bases: [C, F], selected: null }
              : path.endsWith(`/reviews/${REVIEW}`) ? { ...ticket(), current: !stale && !merged }
                : path === `/merge-proposals/${PROPOSAL}` ? current()
                  : path.startsWith('/commits/') && path.endsWith('/artifacts') ? { items: [{ id: OURS, relative_path: 'report.md', object_id: blob }], next_after_id: null }
                    : path.startsWith('/commits/') ? { manifest: { parents: path.endsWith(M) ? [A, B] : [] }, snapshot: { data: { kind: path.endsWith(C) || path.endsWith(M) ? 'merged_research_state/v1' : 'research_state/v1', source_usage: [{ usage: null, execution: 'not_executed' }] } } }
                      : { items: [] };
    response.end(JSON.stringify(result));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening'); const address = server.address(); if (!address || typeof address === 'string') throw new Error('测试端点不可用');
  const folder = await mkdtemp(join(tmpdir(), 'agentgit-s11-merge-')), profile = join(folder, 'profile'), url = `http://127.0.0.1:${address.port}`, token = 'fixture-merge-token-00000000000000000000';
  let desktop = await launchDesktop(profile, url, token);
  try {
    let page = await desktop.firstWindow(); page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); });
    const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(REPO); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '合并审阅', exact: true }).click(); await page.getByText('创建合并提案', { exact: true }).click();
    await page.getByLabel('目标 ours分支', { exact: true }).selectOption('ours'); await page.getByLabel('来源 theirs分支', { exact: true }).selectOption('theirs');
    await page.getByLabel('目标 ours来源 Run UUID', { exact: true }).fill(OURS); await page.getByLabel('来源 theirs来源 Run UUID', { exact: true }).fill(THEIRS); await page.getByLabel('冻结验收器摘要', { exact: true }).fill(F); await expect(page.getByRole('button', { name: '创建固定来源提案', exact: true })).toBeDisabled(); await page.getByLabel('共同祖先', { exact: true }).selectOption(C);
    await page.getByRole('button', { name: '创建固定来源提案', exact: true }).dblclick(); await expect(page.getByRole('button', { name: '用原键确认请求', exact: true })).toBeEnabled(); expect(posts).toHaveLength(1);
    await page.getByRole('button', { name: '用原键确认请求', exact: true }).click(); await expect(page.getByRole('heading', { name: '当前提案 · 候选待验证', exact: true })).toBeVisible(); expect(posts[0]).toEqual(posts[1]);
    await page.getByRole('button', { name: '读取 report.md 原文', exact: true }).first().click(); await expect(page.getByText('<img src=x onerror="window.bad=1">原始文本', { exact: false })).toBeVisible(); expect(await page.locator('.merge-review img').count()).toBe(0);
    await expect(page.getByText('批准后的状态根 · 未执行', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '验证当前修订', exact: true }).click(); await expect(page.getByRole('button', { name: '核对并获取本人票据', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '核对并获取本人票据', exact: true }).click(); await page.getByRole('checkbox', { name: '我已核对三方来源', exact: false }).check(); await expect(page.getByRole('button', { name: '批准合并当前候选', exact: true })).toBeEnabled();
    const reviewCount = posts.length; await desktop.close(); desktop = await launchDesktop(profile, url, token); page = await desktop.firstWindow(); page.on('pageerror', error => errors.push(error.message)); await desktopIdentity(desktop, page); await page.getByRole('button', { name: '合并审阅', exact: true }).click(); await page.getByRole('button', { name: PROPOSAL.slice(0, 12), exact: true }).click(); await expect(page.getByRole('button', { name: '核对并获取本人票据', exact: true })).toBeEnabled(); await expect(page.getByRole('button', { name: '批准合并当前候选', exact: true })).toBeDisabled(); expect(posts).toHaveLength(reviewCount);
    await page.getByRole('button', { name: '核对并获取本人票据', exact: true }).click(); await page.getByRole('checkbox', { name: '我已核对三方来源', exact: false }).check();
    await page.getByLabel('report.md 编辑方式', { exact: true }).selectOption('write'); await page.getByLabel('report.md 完整文本', { exact: true }).fill('## 修订\n显式完整文本'); await expect(page.getByRole('button', { name: '批准合并当前候选', exact: true })).toBeDisabled();
    offline = true; await expect(page.getByRole('button', { name: 'API 已连接', exact: true })).toHaveCount(0); await expect(page.getByLabel('report.md 完整文本', { exact: true })).toHaveValue('## 修订\n显式完整文本'); const offlineCount = posts.length;
    offline = false; await expect(page.getByRole('button', { name: 'API 已连接', exact: true })).toBeVisible(); expect(posts).toHaveLength(offlineCount);
    await page.getByRole('button', { name: '保存为新修订', exact: true }).click(); await expect(page.getByRole('heading', { name: '当前提案 · 候选待验证', exact: true })).toBeVisible(); await expect(page.getByRole('button', { name: '验证当前修订', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '验证当前修订', exact: true }).click(); await expect(page.getByRole('button', { name: '核对并获取本人票据', exact: true })).toBeEnabled(); await page.getByRole('button', { name: '核对并获取本人票据', exact: true }).click(); await page.getByRole('checkbox', { name: '我已核对三方来源', exact: false }).check();
    await page.getByRole('button', { name: '批准合并当前候选', exact: true }).click(); await expect(page.getByText('fixture 来源版本前进', { exact: false })).toBeVisible(); await expect(page.getByRole('heading', { name: '当前提案 · 来源已变化', exact: true })).toBeVisible(); await expect(page.getByRole('button', { name: '批准合并当前候选', exact: true })).toBeDisabled();
    stale = false; await page.getByRole('button', { name: '只读刷新', exact: true }).click(); await expect(page.getByRole('button', { name: '核对并获取本人票据', exact: true })).toBeEnabled(); await page.getByRole('button', { name: '核对并获取本人票据', exact: true }).click(); await page.getByRole('checkbox', { name: '我已核对三方来源', exact: false }).check();
    await page.getByRole('button', { name: '批准合并当前候选', exact: true }).click(); await expect(page.getByRole('button', { name: '用原键确认请求', exact: true })).toBeEnabled(); await expect(page.getByRole('heading', { name: '已合并 · 后续执行须新建任务', exact: true })).toBeVisible(); expect(queued).toBe(false); expect(branched).toBe(false);
    await page.getByRole('button', { name: '用原键确认请求', exact: true }).click(); const approvals = posts.filter(item => item.path.endsWith('/approve')); expect(approvals.at(-1)).toEqual(approvals.at(-2));
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0)); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const file = join(folder, `merge-${width}.png`); await captureDesktop(desktop, page, file, true); images.push(file); }
    await page.getByLabel('新分支名称', { exact: true }).fill('merged/fixture'); await page.getByRole('button', { name: '建立独立合并分支', exact: true }).click(); await page.getByLabel('Agent 配置 UUID', { exact: true }).fill(OURS); await page.getByRole('checkbox', { name: '我选择此配置和预算', exact: false }).check(); await page.getByRole('button', { name: '提交独立新任务', exact: true }).click(); await expect(page.getByRole('heading', { name: '新任务已提交', exact: true })).toBeVisible(); expect(queued).toBe(true);
    expect(await page.evaluate(() => 'bad' in window)).toBe(false); expect(errors).toEqual([]);
    const count = posts.length; await desktop.close(); desktop = await launchDesktop(profile, url, token); page = await desktop.firstWindow(); await desktopIdentity(desktop, page); await page.getByRole('button', { name: '合并审阅', exact: true }).click(); await page.getByRole('button', { name: PROPOSAL.slice(0, 12), exact: true }).click(); await expect(page.getByRole('heading', { name: '已合并 · 后续执行须新建任务', exact: true })).toBeVisible(); expect(posts).toHaveLength(count); await expect(page.getByRole('button', { name: '批准合并当前候选', exact: true })).toBeDisabled();
    expect(posts.filter(item => item.path === '/merge-runs')).toHaveLength(1); expect(posts.every(item => item.key)).toBe(true);
    await writeFile(join(folder, 'merge.json'), JSON.stringify({ scope: 'explicit-http-fixture', runtime, posts, images, errors }, null, 2)); console.log(`合并桌面 fixture 证据：${folder}`);
  } finally { try { await desktop.close(); } finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); } }
});
