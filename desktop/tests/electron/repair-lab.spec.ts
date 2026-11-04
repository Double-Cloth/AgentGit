import { test, expect } from '@playwright/test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const REPO = '00000000-0000-4000-8000-000000000001', RUN = '10000000-0000-4000-8000-000000000001';
const SEARCH = '11111111-0000-4000-8000-000000000001', OBS = '22222222-0000-4000-8000-000000000001';
const STUDY = '33333333-0000-4000-8000-000000000001', SUITE = '44444444-0000-4000-8000-000000000001', PROPOSAL = '55555555-0000-4000-8000-000000000001';

test('Repair Lab 可见流程：原键确认、缩减复测、版本冲突、观测禁采纳和重开只读', async () => {
  let phase = 0, lose = true, version = 29, observed = false, unavailable = false, offline = false, conflict = false, decided: unknown = null;
  const posts: { path: string; key: string | undefined; body: Record<string, unknown> }[] = [];
  const verification = () => ({ status: 'verified_under_fixture', lock_ref: 'c'.repeat(64), confirm_determinate: 6, confirm_passed: 6, regression_planned: 4, regression_passed: 4, production_eligible: !observed, has_observation_patch: observed, confirmation_interval: { lower: '0.609665712097834617', upper: '1.000000000000000000' } });
  const proposal = () => ({ source_config_id: RUN, source_config_version: 1, before: { prompt: '<img src=x onerror="window.bad=1">来源规则', plan_template: null }, after: observed ? null : { prompt: '仅引用已读取来源' }, adoption_scope: observed ? 'save_experiment_only' : 'create_config_version', verification_ref: 'b'.repeat(64), verification: verification(), patches: [{ operator: observed ? 'TOOL_RESPONSE_PATCH' : 'PROMPT_PATCH' }] });
  const ticket = () => ({ review_id: SUITE, ticket_ref: 'd'.repeat(64), ticket: { proposal_id: PROPOSAL, proposal_ref: 'a'.repeat(64), source_config_id: RUN, source_config_version: 1, verification_ref: 'b'.repeat(64), lock_ref: 'c'.repeat(64), expected_config_version: version } });
  const usage = { model_calls: 56, tool_calls: 40, tokens: 592252, cost_micro_usd: 0, active: 0, elapsed_wall_ms: 50474 };
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json'); const path = new URL(request.url!, 'http://localhost').pathname;
    if (request.method === 'POST') {
      const chunks: Buffer[] = []; request.on('data', chunk => chunks.push(chunk)); request.on('end', () => {
        const body = JSON.parse(Buffer.concat(chunks).toString()) as Record<string, unknown>; posts.push({ path, key: request.headers['idempotency-key'] as string | undefined, body });
        if (path === '/repair-searches/prepared') { phase = 1; if (lose) { lose = false; response.writeHead(503); response.end(JSON.stringify({ error: { code: 'fixture_unavailable', message: '测试已提交但丢失响应' } })); return; } response.end(JSON.stringify({ search_id: SEARCH, validation_suite: { suite_id: SUITE } })); }
        else if (path === '/repair-studies') { phase = 2; response.end(JSON.stringify({ study_id: STUDY })); }
        else if (path.endsWith('/open-validation')) { phase = 3; response.end(JSON.stringify({ opened: true })); }
        else if (path.endsWith('/proposal')) { phase = 4; response.end(JSON.stringify({ proposal_id: PROPOSAL })); }
        else if (path.endsWith('/review')) response.end(JSON.stringify(ticket()));
        else if (conflict) { conflict = false; version++; response.writeHead(409); response.end(JSON.stringify({ error: { code: 'version_conflict', message: 'fixture version advanced' } })); }
        else { decided = { decision: body.decision, new_config_version: observed ? null : version + 1, execution: 'not_started' }; response.end(JSON.stringify({ decision: decided })); }
      }); return;
    }
    if (offline && path === '/ready' || unavailable && path === `/repair-searches/${OBS}`) { response.writeHead(503); response.end(JSON.stringify({ error: { code: 'fixture_unavailable', message: 'fixture read unavailable' } })); return; }
    const selected = path.includes(OBS) ? OBS : SEARCH;
    const data = path === '/health' ? { status: 'ok', version: '0.1.0' } : path === '/ready' ? { status: 'ready' }
      : path === '/capabilities' ? { schema_version: 1, version: '0.1.0', features: ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff', 'prepared_repair_search', 'repair_workflow'] }
      : path === `/repositories/${REPO}` ? { id: REPO }
      : path === `/runs/${RUN}` ? { id: RUN, status: 'failed', model: 'mock-research/v1' }
      : path.startsWith('/validators/') ? { validator_ref: 'b'.repeat(64), manifest: { fixture_split: 'screen' } }
      : path.endsWith('/repair-searches') ? { items: phase ? [{ search_id: SEARCH, failed_run_id: RUN, status: 'needs_confirmation' }, { search_id: OBS, failed_run_id: RUN, status: 'needs_confirmation' }] : [], has_more: false }
      : path.endsWith('/workflow') ? { search_id: selected, suite_id: SUITE, study_id: phase >= 2 ? STUDY : null, proposal_id: phase >= 4 ? PROPOSAL : null }
      : path.startsWith('/repair-searches/') ? { search_id: selected, status: 'needs_confirmation', usage, manifest: { failure_assertions: ['missing_artifacts'] }, trials: [], proposals: [] }
      : path === `/repair-studies/${STUDY}` ? { study_id: STUDY, status: phase >= 3 ? 'verified_under_fixture' : 'awaiting_samples', usage, trials: [], reduction: { minimality: 'one_minimal_under_fixture' }, final_lock: { lock_ref: 'c'.repeat(64) } }
      : path.endsWith('/validation') ? { validation: phase >= 3 ? { opened: true } : null, execution: phase >= 3 ? { status: 'verified_under_fixture', usage, verification: verification(), trials: [] } : null }
      : path === `/repair-proposals/${PROPOSAL}` ? { proposal_id: PROPOSAL, proposal_ref: 'a'.repeat(64), current_config_version: version, proposal: proposal(), review: decided }
      : { items: [] };
    response.end(JSON.stringify(data));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening'); const address = server.address(); if (!address || typeof address === 'string') throw new Error('测试端点不可用');
  const folder = await mkdtemp(join(tmpdir(), 'agentgit-s10-lab-')), profile = join(folder, 'profile'), images: string[] = [];
  let desktop = await launchDesktop(profile, `http://127.0.0.1:${address.port}`, 'fixture-lab-token-00000000000000000000');
  const errors: string[] = [];
  try {
    let page = await desktop.firstWindow(); page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (['error', 'warning'].includes(entry.type())) errors.push(entry.text()); });
    const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(REPO); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '修复实验室', exact: true }).click(); await expect(page.getByRole('heading', { name: '修复实验室', exact: true })).toBeVisible();
    await page.getByLabel('失败 Run UUID', { exact: true }).fill(RUN); await page.getByLabel('模型决策前起点', { exact: true }).fill('a'.repeat(64)); await page.getByLabel('screen 验收器摘要', { exact: true }).fill('b'.repeat(64));
    await page.getByLabel('有限事件目标 JSON', { exact: false }).fill('[{"schema_version":1,"event_id":9100}]');
    for (let i = 1; i <= 4; i++) { await page.getByLabel(`回归 ${i} Run UUID`, { exact: true }).fill(`90000000-0000-4000-8000-${String(i).padStart(12, '0')}`); await page.getByLabel(`回归 ${i} 模型决策前起点`, { exact: true }).fill('c'.repeat(64)); }
    await page.getByRole('checkbox', { name: '我已核对来源', exact: false }).check();
    await page.getByRole('button', { name: '准备并搜索最小修复', exact: true }).dblclick(); await expect(page.getByRole('button', { name: '用原键确认请求', exact: true })).toBeEnabled(); expect(posts).toHaveLength(1);
    await page.getByRole('button', { name: '用原键确认请求', exact: true }).click(); await expect(page.getByRole('button', { name: '开始缩减', exact: true })).toBeEnabled(); expect(posts[0]).toEqual(posts[1]); await expect(page.getByText('missing_artifacts', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '开始缩减', exact: true }).click(); await expect(page.getByRole('button', { name: '锁后开启独立复测', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '锁后开启独立复测', exact: true }).click(); await expect(page.getByText('确认 6 / 6 · 回归 4 / 4', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '生成精确修复建议', exact: true }).click(); await expect(page.getByRole('heading', { name: '精确修复建议', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '核对并获取审阅票据', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeEnabled();
    version = 30; await page.getByRole('button', { name: '只读刷新', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeDisabled(); await expect(page.getByText('配置版本或建议绑定已变化。', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: '核对并获取审阅票据', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeEnabled(); conflict = true; await page.getByRole('button', { name: '批准创建配置版本', exact: true }).click(); await expect(page.getByText('fixture version advanced', { exact: false })).toBeVisible(); await page.getByRole('button', { name: '核对并获取审阅票据', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeEnabled(); await page.getByRole('button', { name: '批准创建配置版本', exact: true }).click(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible();
    expect(posts.at(-1)!.body.expected_config_version).toBe(31); expect(posts.at(-1)!.body.decision).toBe('approve');
    observed = true; decided = null; await page.locator('.experiment-list button').filter({ hasText: '22222222' }).click(); await expect(page.getByText('包含工具观测替换：test_only', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: '核对并获取审阅票据', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeDisabled(); await expect(page.getByRole('button', { name: '仅保存为实验', exact: true })).toBeEnabled();
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0)); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const file = join(folder, `lab-${width}.png`); await captureDesktop(desktop, page, file, true); images.push(file); }
    expect(await page.locator('.repair-lab img').count()).toBe(0); expect(await page.evaluate(() => 'bad' in window)).toBe(false);
    await page.getByRole('button', { name: '仅保存为实验', exact: true }).click(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible(); expect(posts.at(-1)!.body.decision).toBe('save_experiment');
    const beforeRefresh = posts.length; unavailable = true; await page.getByRole('button', { name: '只读刷新', exact: true }).click(); await expect(page.getByText('显示上次已确认结果', { exact: false })).toBeVisible(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible(); unavailable = false; await page.getByRole('button', { name: '只读刷新', exact: true }).click(); await expect(page.getByText('显示上次已确认结果', { exact: false })).toHaveCount(0);
    offline = true; await expect(page.getByRole('button', { name: '只读刷新', exact: true })).toBeDisabled(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible(); offline = false; await expect(page.getByRole('button', { name: '只读刷新', exact: true })).toBeEnabled(); expect(posts).toHaveLength(beforeRefresh);
    const count = posts.length; await desktop.close(); desktop = await launchDesktop(profile, `http://127.0.0.1:${address.port}`, 'fixture-lab-token-00000000000000000000'); page = await desktop.firstWindow(); await desktopIdentity(desktop, page);
    await page.getByRole('button', { name: '修复实验室', exact: true }).click(); await page.locator('.experiment-list button').filter({ hasText: '22222222' }).click(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible(); expect(posts).toHaveLength(count);
    expect(await page.locator('vite-error-overlay').count()).toBe(0); expect(posts.some(row => ['/runs', '/tasks', '/replays'].includes(row.path))).toBe(false); expect(errors).toEqual([]);
    await writeFile(join(folder, 'lab.json'), JSON.stringify({ runtime, posts, images, errors, scope: 'explicit-http-fixture' }, null, 2)); console.log(`Repair Lab 证据：${folder}`);
  } finally { try { await desktop.close(); } finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); } }
});
