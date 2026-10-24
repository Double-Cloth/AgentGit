import { test, expect } from '@playwright/test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const REPO = '00000000-0000-4000-8000-000000000001', RUN = '00000000-0000-4000-8000-000000000002', REF = '00000000-0000-4000-8000-000000000003';
const REPORT = '00000000-0000-4000-8000-000000000004', EXP = '00000000-0000-4000-8000-000000000005';
const event = { run_id: RUN, seq: 3001, event_id: 9101 }, assertion = { run_id: RUN, seq: 3003, event_id: 9103 };

test('HTTP 边界：报告原键确认、并列缺口、安全文本、精确旧事件及重开只读', async () => {
  let lose = true, corrupt = false, complete = false, reportUnavailable = false, reject = true;
  const posts: { path: string; key: string | undefined; body: unknown }[] = [], reads: string[] = [];
  const candidate = { event, rank: 1, tied_count: 2, kind: 'accepted_tool_decision', action_step: 'write-report', action_provider: 'tool:artifact_write/v1', supporting_events: [event, { run_id: RUN, seq: 3002 }], features: { failure_path: true }, reasons: ['沿失败硬断言依赖路径', '<img src=x onerror="window.bad=1">'], uncertainties: ['父运行读取未展开，不能证明根因'] };
  const stored = { debug_report_id: REPORT, report_ref: 'b'.repeat(64), report: { failed: { run_id: RUN, gaps: [{ event, reason: 'outside_trace' }] }, reference: { run_id: REF }, environment_changes: [],
    comparison: { status: 'comparable', matches: [{ left: event, right: { ...event, run_id: REF }, comparison: 'changed', basis: 'logical_identity', differences: ['observation'] }], ambiguities: [{ left: [event], right: [{ ...event, run_id: REF }] }], first_left: [event], first_right: [{ ...event, run_id: REF }] },
    diagnosis: { complete_evidence: false, status: 'candidates_available', candidates: [candidate, { ...candidate, event: { ...event, seq: 3002, event_id: 9102 }, action_step: 'write-sources' }], assertions: [{ event: assertion, code: 'missing_artifacts', origin: 'research_validator' }], observed_anomalies: [], limitations: ['静态排序不是因果分数，改善未知'] } } };
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json'); const url = new URL(request.url!, 'http://localhost'), path = url.pathname;
    if (request.method === 'POST') {
      const chunks: Buffer[] = []; request.on('data', chunk => chunks.push(chunk)); request.on('end', () => {
        posts.push({ path, key: request.headers['idempotency-key'] as string | undefined, body: JSON.parse(Buffer.concat(chunks).toString()) });
        if (reject) { reject = false; response.writeHead(422); response.end(JSON.stringify({ error: { code: 'validation', message: '测试输入已明确拒绝' } })); }
        else if (lose) { lose = false; response.writeHead(503); response.end(JSON.stringify({ error: { code: 'fixture_unavailable', message: '报告已提交但测试丢失响应' } })); }
        else { response.writeHead(201); response.end(JSON.stringify({ debug_report_id: REPORT, report_ref: 'b'.repeat(64), mode: 'static_only' })); }
      }); return;
    }
    reads.push(request.url!);
    if (path === `/debug-reports/${REPORT}` && reportUnavailable) { response.writeHead(503); response.end(JSON.stringify({ error: { code: 'fixture_unavailable', message: '测试报告暂不可读' } })); return; }
    const data = path === '/health' ? { status: 'ok', version: '0.1.0' } : path === '/ready' ? { status: 'ready' }
      : path === '/capabilities' ? { schema_version: 1, version: '0.1.0', features: ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff', 'static_debug_reports'] }
      : path === `/repositories/${REPO}` ? { id: REPO }
      : path.endsWith('/branches') ? { items: [{ name: 'main' }] } : path.endsWith('/commits') ? { items: [] }
      : path === `/repositories/${REPO}/debug-reports` ? { items: lose ? [] : [{ debug_report_id: REPORT, failed_run_id: RUN, reference_run_id: REF }], has_more: false }
      : path === `/debug-reports/${REPORT}` ? stored
      : path.endsWith('/candidate-context') ? { request_event: { run_id: RUN, seq: 3000, event_id: 9100 }, context: { source_run_id: RUN, target_event_id: 9100, base_commit: 'a'.repeat(64) } }
      : path.endsWith('/candidate-experiment') ? { effect_status: complete ? 'assessed' : 'incomplete', success_rate_difference: complete ? { numerator: '1', denominator: '1' } : null, experiment: { statistics: { groups: [] }, trials: [{ group: 'A', index: 0, status: complete ? 'completed' : 'cancelled', verdict: complete ? 'passed' : 'incomplete', run_id: RUN, job_id: REPORT }] }, limitations: ['单变量条件比较，不证明严格因果'] }
      : path === `/runs/${RUN}` ? { id: RUN, branch_name: 'main', status: 'failed', state_version: 1 }
      : path === `/runs/${RUN}/events` ? { items: Number(url.searchParams.get('after_seq')) === 3000 ? [{ id: corrupt ? 9102 : 9101, run_id: RUN, seq: 3001, type: 'llm.completed', logical_step_id: 'write-report', payload: { evidence: '<script>window.bad=1</script>' } }] : [], next_after_seq: 5000 }
      : { items: [] };
    response.end(JSON.stringify(data));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening'); const address = server.address(); if (!address || typeof address === 'string') throw new Error('测试端点不可用');
  const evidence = await mkdtemp(join(tmpdir(), 'agentgit-s8-boundary-')), profile = join(evidence, 'profile'), screenshots: string[] = [];
  let desktop = await launchDesktop(profile, `http://127.0.0.1:${address.port}`, 'fixture-diagnosis-token-00000000000000000000');
  const errors: string[] = [];
  try {
    let page = await desktop.firstWindow(); page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (['error', 'warning'].includes(entry.type())) errors.push(entry.text()); });
    const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(REPO); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '差异', exact: true }).click(); await page.getByRole('tab', { name: '故障定位', exact: true }).press('Enter');
    await page.getByLabel('失败 Run UUID', { exact: true }).fill(RUN); await page.getByLabel('成功参考 Run UUID（可选）').fill(REF);
    await page.getByRole('button', { name: '生成静态报告', exact: true }).click(); await expect(page.getByText('测试输入已明确拒绝', { exact: false })).toBeVisible(); await expect(page.getByLabel('失败 Run UUID', { exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '生成静态报告', exact: true }).dblclick(); await expect(page.getByRole('button', { name: '继续原报告请求', exact: true })).toBeEnabled(); expect(posts).toHaveLength(2);
    await expect(page.getByLabel('失败 Run UUID', { exact: true })).toBeDisabled();
    await page.getByRole('button', { name: '继续原报告请求', exact: true }).press('Enter'); await expect(page.getByRole('heading', { name: '冻结静态报告', exact: true })).toBeVisible();
    expect(posts).toHaveLength(3); expect(posts[1]).toEqual(posts[2]); expect(posts[0]!.key).not.toBe(posts[1]!.key);
    reportUnavailable = true; await page.getByRole('button', { name: '打开报告', exact: true }).click(); await expect(page.getByText('测试报告暂不可读', { exact: false })).toBeVisible();
    reportUnavailable = false; await page.getByRole('button', { name: '打开报告', exact: true }).click(); await expect(page.getByRole('heading', { name: '冻结静态报告', exact: true })).toBeVisible();
    await expect(page.getByText('排名 1 · 2 项并列', { exact: true })).toHaveCount(2);
    await expect(page.getByText('<img src=x onerror="window.bad=1">', { exact: true })).toBeVisible(); expect(await page.locator('article img').count()).toBe(0);
    await page.getByText('对齐歧义（1 项）', { exact: true }).click(); await expect(page.getByText('重复身份未消歧', { exact: true })).toBeVisible();
    await page.getByText('已对齐事件（1 项）', { exact: true }).click(); await expect(page.getByText('changed · logical_identity', { exact: false })).toBeVisible();
    for (const width of [1440, 1024, 768, 360]) {
      await page.setViewportSize({ width, height: 1000 }); await page.getByRole('heading', { name: '冻结静态报告', exact: true }).scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      const path = join(evidence, `report-${width}.png`); await page.screenshot({ path, fullPage: true }); screenshots.push(path);
    }
    await page.getByLabel('候选验收器摘要').fill('c'.repeat(64)); await page.getByRole('button', { name: '核对干预上下文', exact: true }).click(); await expect(page.getByText('已核对的调用与检查点', { exact: true })).toBeVisible();
    await page.getByLabel('候选 Experiment UUID').fill(EXP); await page.getByRole('button', { name: '核对实验效果', exact: true }).click(); await expect(page.getByText('试验尚未完整判定 · 改善未知', { exact: true })).toBeVisible();
    complete = true; await page.getByRole('button', { name: '核对实验效果', exact: true }).click(); await expect(page.getByText('两组均已判定 · 成功率差 1', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '事件 3001 · ID 9101', exact: true }).first().click(); await expect(page.getByRole('heading', { name: '定位证据 · 事件 3001', exact: true })).toBeFocused();
    await expect(page.locator('.focused-event')).toContainText('window.bad=1'); expect(await page.evaluate(() => 'bad' in window)).toBe(false);
    expect(reads.some(path => path === `/runs/${RUN}/events?after_seq=3000&limit=1`)).toBe(true);
    corrupt = true; await page.getByRole('button', { name: '查看故障报告', exact: true }).click(); await page.locator('.debug-report-list button').first().click(); await page.getByRole('button', { name: '事件 3001 · ID 9101', exact: true }).first().click();
    await expect(page.getByText('报告引用的精确事件不存在或身份不符', { exact: false })).toBeVisible(); await expect(page.locator('.focused-event pre')).toHaveCount(0);
    await desktop.close(); desktop = await launchDesktop(profile, `http://127.0.0.1:${address.port}`, 'fixture-diagnosis-token-00000000000000000000'); page = await desktop.firstWindow(); await desktopIdentity(desktop, page);
    await page.getByRole('button', { name: '差异', exact: true }).click(); await page.getByRole('tab', { name: '故障定位', exact: true }).click(); await page.locator('.debug-report-list button').first().click(); await expect(page.getByRole('heading', { name: '冻结静态报告', exact: true })).toBeVisible(); expect(posts).toHaveLength(3); expect(errors).toEqual([]);
    await writeFile(join(evidence, 'boundary.json'), JSON.stringify({ runtime, posts, reads, screenshots, errors }, null, 2)); console.log(`S8 边界证据：${evidence}`);
  } finally { try { await desktop.close(); } finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); } }
});
