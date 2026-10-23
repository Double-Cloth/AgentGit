import { test, expect, _electron as electron } from '@playwright/test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const REPO = '00000000-0000-4000-8000-000000000001', RUN = '00000000-0000-4000-8000-000000000002';
const EXP = '00000000-0000-4000-8000-000000000003', JOB = '00000000-0000-4000-8000-000000000004', BASE = 'a'.repeat(64), REF = 'b'.repeat(64);

test('HTTP fixture：费用未知、旧验收器、断连保留与控制原键重试', async () => {
  let available = true, loseControl = true, status = 'queued', version = 7, incompatible = false;
  const controls: { path: string; key: string | undefined; body: unknown }[] = [], posts: string[] = [];
  const limits = { schema_version: 1, max_steps: 32, max_model_calls: 32, max_tool_calls: 16, max_cost_micro_usd: 1000000 };
  const ratio = { numerator: '0', denominator: '1' };
  const metrics = Object.fromEntries(['actual.model_calls', 'actual.tool_calls', 'actual.wall_ms', 'mock.total_bytes', 'provider.input_tokens', 'provider.output_tokens', 'provider.estimated_cost_micro_usd', 'provider.known_estimated_cost_micro_usd', 'provider.reserved_cost_micro_usd', 'provider.billed_cost_micro_usd'].map(name => [name, { samples: 0, missing_samples: 5, not_applicable_samples: 0, sum: null, mean: null, sample_variance: null }]));
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json'); const path = new URL(request.url!, 'http://localhost').pathname;
    if (!available) { response.writeHead(503); response.end(JSON.stringify({ error: { code: 'fixture_unavailable', message: '测试断连' } })); return; }
    if (request.method === 'POST') {
      posts.push(path); const chunks: Buffer[] = []; request.on('data', chunk => chunks.push(chunk)); request.on('end', () => {
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (path.endsWith('/pause')) {
          controls.push({ path, key: request.headers['idempotency-key'] as string | undefined, body });
          if (loseControl) { loseControl = false; response.writeHead(503); response.end(JSON.stringify({ error: { code: 'fixture_unavailable', message: '控制已受理但测试丢失响应' } })); return; }
          status = 'paused'; version += 1;
        }
        response.end(JSON.stringify(path === '/validators' ? { validator_ref: REF } : path === '/experiments' ? { experiment_id: EXP } : { run_id: RUN }));
      }); return;
    }
    if (path === `/experiments/${EXP}` && incompatible) { response.writeHead(422); response.end(JSON.stringify({ error: { code: 'unsupported_schema', message: '冻结验收器不兼容' } })); return; }
    const run = { id: RUN, branch_name: 'main', status, state_version: version, model_external: true, model: 'openai/fixed', budget: { limits } };
    const result = path === '/health' ? { status: 'ok', version: '0.1.0' } : path === '/ready' ? { status: 'ready' }
      : path === '/capabilities' ? { schema_version: 1, version: '0.1.0', features: ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff', 'controlled_experiments', 'experiment_statistics'] }
      : path === `/repositories/${REPO}` ? { id: REPO, name: '边界实验', config_version: 0 }
      : path.endsWith('/runs') ? { items: [run] } : path === `/runs/${RUN}` ? run
      : path.endsWith('/commits') ? { items: [{ commit_id: BASE }] }
      : path === `/validators/${REF}` ? { validator_ref: REF, manifest: { kind: 'frozen_research_validator/v1' } }
      : path.endsWith('/experiments') ? { items: [{ id: EXP, source_run_id: RUN, from_commit_id: BASE, trial_count: 10 }], has_more: false }
      : path === `/experiments/${EXP}` ? { experiment_id: EXP, manifest: { mode: 'live', test_only: true }, test_only: true,
        trials: [{ group: 'A', index: 0, run_id: RUN, initial_run_id: RUN, job_id: JOB, attempt: 1, status, verdict: 'incomplete', replay: { actual: { model_calls: 1, tool_calls: 0, model_usage: { mock: { calls: 0 }, provider: { calls: 1, estimated_cost_micro_usd: null, reserved_cost_micro_usd: 1000000 } } } } }],
        statistics: { groups: ['A', 'B'].map(key => ({ key, passed: 0, failed: 0, incomplete: 5, planned_trials: 5, success_rate: ratio, hard_assertions: { not_evaluated: 5 }, recovery: { trials: 0, passed: 0 }, consumption_to_date: { trial_count: 5, metrics }, assessed_trials: { trial_count: 0, metrics: {} } })) } }
      : { items: [] };
    response.end(JSON.stringify(result));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening'); const address = server.address(); if (!address || typeof address === 'string') throw new Error('测试端点不可用');
  const profile = await mkdtemp(join(tmpdir(), 'agentgit-s7-boundary-'));
  const desktop = await electron.launch({ args: ['.'], cwd: resolve('.'), env: { ...process.env, AGENTGIT_API_TOKEN: 'fixture-boundary-token-00000000000000000000', AGENTGIT_API_URL: `http://127.0.0.1:${address.port}`, AGENTGIT_DESKTOP_PROFILE_DIR: profile } });
  try {
    const page = await desktop.firstWindow(), errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    await page.getByLabel('已有项目 ID').fill(REPO); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '分支实验', exact: true }).click(); await page.getByRole('button', { name: '受控 A/B 实验', exact: true }).press('Enter');
    await page.getByLabel('实验来源 Run', { exact: true }).fill(RUN); await page.getByLabel('实验安全起点 Commit').fill(BASE);
    await page.getByRole('button', { name: '冻结共同验收器', exact: true }).press('Enter'); await expect(page.getByLabel('每个 trial 费用上限（USD）')).toBeVisible();
    await page.getByRole('button', { name: '提交 A/B 实验', exact: true }).click(); await expect(page.getByText('请输入 USD 金额，例如 1.00', { exact: false })).toBeVisible(); expect(posts.filter(path => path === '/experiments')).toHaveLength(0);
    await page.getByLabel('每个 trial 费用上限（USD）').fill('0.50'); await page.getByRole('button', { name: '提交 A/B 实验', exact: true }).click();
    await expect(page.getByRole('cell').filter({ hasText: '估价 未知' })).toContainText('预留 USD 1');
    await page.getByLabel('统计范围').selectOption('assessed_trials'); await expect(page.getByText('此范围尚无完整试验，均值和方差未知。', { exact: true })).toHaveCount(2);
    await page.getByRole('button', { name: '暂停 A · 1', exact: true }).click(); await expect(page.getByRole('button', { name: '继续原暂停请求', exact: true })).toBeVisible();
    available = false; await expect(page.getByRole('button', { name: '连接设置', exact: true })).toBeVisible({ timeout: 15000 });
    available = true; await expect(page.getByRole('button', { name: 'API 已连接', exact: true })).toBeVisible({ timeout: 15000 });
    await page.getByRole('button', { name: '继续原暂停请求', exact: true }).click(); await expect(page.getByRole('button', { name: '恢复 A · 1', exact: true })).toBeEnabled();
    expect(controls).toHaveLength(2); expect(controls[0]).toEqual(controls[1]); expect(controls[0]!.body).toMatchObject({ expected_version: 7 });
    incompatible = true; await page.getByRole('button', { name: '刷新试验状态', exact: true }).click(); await expect(page.getByText('显示上次已确认结果；刷新失败：冻结验收器不兼容', { exact: false })).toBeVisible();
    incompatible = false; await page.getByLabel('Experiment UUID', { exact: true }).press('Enter'); await expect(page.getByText('显示上次已确认结果；刷新失败：冻结验收器不兼容', { exact: false })).toHaveCount(0);
    await page.getByRole('button', { name: '开始另一实验', exact: true }).click();
    expect(await page.getByLabel('实验安全起点 Commit').evaluate(input => getComputedStyle(input.parentElement!).marginTop)).toBe('20px');
    for (const width of [1440, 1024, 768, 360]) {
      await page.setViewportSize({ width, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0));
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      await page.screenshot({ path: `../.agentgit/evidence/S7-desktop-form-${width}.png` });
      await page.getByRole('heading', { name: '独立试验', exact: true }).scrollIntoViewIfNeeded();
      await page.screenshot({ path: `../.agentgit/evidence/S7-desktop-unknown-${width}.png` });
    }
    expect(posts.filter(path => path === '/experiments')).toHaveLength(1); expect(errors).toEqual([]);
  } finally { try { await desktop.close(); } finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); } }
});
