import { test, expect, type Page } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, data, objects, startBackend } from '../support/real-client';
import { desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const object = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};

async function prepare(page: Page, seed: JsonObject) {
  await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
  await page.getByRole('button', { name: '修复实验室', exact: true }).click();
  await page.getByLabel('失败 Run UUID', { exact: true }).fill(String(seed.failed_run)); await page.getByLabel('模型决策前起点', { exact: true }).fill(String(seed.base)); await page.getByLabel('screen 验收器摘要', { exact: true }).fill(String(seed.validator));
  await page.getByLabel('有限事件目标 JSON', { exact: false }).fill(JSON.stringify([{ schema_version: 1, event_id: seed.target_event }]));
  const origins = objects(seed.regression);
  for (let i = 0; i < 4; i++) { await page.getByLabel(`回归 ${i + 1} Run UUID`, { exact: true }).fill(String(origins[i]!.source_run_id)); await page.getByLabel(`回归 ${i + 1} 模型决策前起点`, { exact: true }).fill(String(origins[i]!.from_commit)); }
  await page.getByText('完整预算与 Hybrid 条件', { exact: true }).click(); await page.getByLabel('完整修复预算 JSON（Mock 可沿用默认）', { exact: true }).fill(JSON.stringify(seed.budget));
  await page.getByRole('checkbox', { name: '我已核对来源', exact: false }).check(); await page.getByRole('button', { name: '准备并搜索最小修复', exact: true }).click();
  await expect(page.getByLabel('搜索 UUID', { exact: true })).not.toHaveValue(''); return page.getByLabel('搜索 UUID', { exact: true }).inputValue();
}

test('真实排队搜索取消：落实安全终态且不派发试验', async () => {
  const backend = await startBackend('repair-boundaries'), folder = await mkdtemp(join(tmpdir(), 'agentgit-repair-cancel-'));
  const seed = await backend.command('seed').catch(async error => { await backend.stop(); throw error; });
  await backend.command('worker_gate', { enabled: true });
  const desktop = await launchDesktop(join(folder, 'profile'), backend.url, TOKEN).catch(async error => { await backend.stop(); throw error; });
  const errors: string[] = [];
  try {
    const page = await desktop.firstWindow(), runtime = await desktopIdentity(desktop, page); page.on('pageerror', error => errors.push(error.message));
    const before = await backend.command('state'), search = await prepare(page, seed);
    await expect(page.getByRole('button', { name: '取消搜索', exact: true })).toBeEnabled(); await page.getByRole('button', { name: '取消搜索', exact: true }).dblclick(); await backend.command('worker_gate', { enabled: false });
    let report: JsonObject = {};
    await expect.poll(async () => { report = data(await page.evaluate(searchId => window.agentgit.getRepairSearch({ searchId }), search)); return report.status; }).toBe('cancelled');
    await page.getByRole('button', { name: '只读刷新', exact: true }).click(); await expect(page.locator('.experiment-report .section-heading')).toContainText('已取消'); await expect(page.getByRole('button', { name: '开始缩减', exact: true })).toBeDisabled();
    expect(objects(report.trials)).toHaveLength(0); const after = await backend.command('state'); expect(after.counts).toEqual(before.counts); expect(after.source_fingerprint).toBe(seed.source_fingerprint);
    const audit = await backend.command('edge_audit'); expect(objects(object(audit.indexes).jobs)).toHaveLength(0); expect(objects(audit.invocations)).toHaveLength(0);
    expect(objects(after.mutations).filter(row => row.path === `/repair-searches/${search}/cancel`)).toHaveLength(1); expect(errors).toEqual([]); expect(await page.locator('vite-error-overlay').count()).toBe(0);
    await writeFile(join(folder, 'cancel.json'), JSON.stringify({ scope: 'real-postgres-api-worker-cas-electron/mock-model', runtime, seed, search, report, before, after, audit, errors, root: backend.root }, null, 2)); console.log(`真实排队取消证据：${folder}`);
  } finally { try { await desktop.close(); } finally { await backend.stop(); } }
});

test('真实确认结果未知：取消和重开保留一项预留，不重发模型或生成验证结论', async () => {
  test.setTimeout(600000);
  const backend = await startBackend('repair-boundaries'), folder = await mkdtemp(join(tmpdir(), 'agentgit-repair-unknown-'));
  const seed = await backend.command('seed').catch(async error => { await backend.stop(); throw error; });
  const profile = join(folder, 'profile'); let desktop = await launchDesktop(profile, backend.url, TOKEN).catch(async error => { await backend.stop(); throw error; });
  const errors: string[] = [], screenshots: string[] = [];
  function observe(page: Page) { page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); }); }
  try {
    let page = await desktop.firstWindow(); observe(page); const runtime = await desktopIdentity(desktop, page), search = await prepare(page, seed);
    await expect(page.getByRole('button', { name: '开始缩减', exact: true })).toBeEnabled({ timeout: 180000 }); await page.getByRole('button', { name: '开始缩减', exact: true }).click();
    await expect(page.getByRole('button', { name: '锁后开启独立复测', exact: true })).toBeEnabled({ timeout: 240000 }); await page.getByRole('button', { name: '锁后开启独立复测', exact: true }).click();
    await expect(page.getByText('存在结果未知试验：预留继续保留', { exact: false })).toBeVisible({ timeout: 90000 });
    const workflow = data(await page.evaluate(searchId => window.agentgit.getRepairWorkflow({ searchId }), search)), studyId = String(workflow.study_id);
    const read = () => page.evaluate(studyId => window.agentgit.getRepairValidation({ studyId }), studyId).then(data);
    const waiting = await read(), execution = object(waiting.execution), trials = objects(execution.trials);
    expect(execution.status).toBe('waiting_reconciliation'); expect(object(execution.usage).active).toBe(1); expect(execution.verification).toBeNull(); expect(trials).toHaveLength(1); expect(trials[0]!.result).toBeUndefined(); expect(object(trials[0]!.waiting).reason).toBe('outcome_unknown');
    await expect(page.getByRole('button', { name: '生成精确修复建议', exact: true })).toBeDisabled();
    const table = page.getByRole('heading', { name: '独立确认 / 回归 · 1 项', exact: true }).locator('..'); await expect(table).toContainText('结果未知'); await expect(table.locator('tbody tr td:nth-child(3)')).toHaveText('未知 / 未知');
    await page.getByRole('button', { name: '取消研究', exact: true }).dblclick();
    await expect.poll(async () => object((await read()).execution).status).toBe('cancelling');
    const beforeReopen = await backend.command('state'), firstAudit = await backend.command('edge_audit'); expect(objects(firstAudit.invocations)).toHaveLength(1);
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.getByText('存在结果未知试验：预留继续保留', { exact: false }).scrollIntoViewIfNeeded(); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const path = join(folder, `unknown-${width}.png`); await page.screenshot({ path }); screenshots.push(path); }
    await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); observe(page); await desktopIdentity(desktop, page);
    await page.getByRole('button', { name: '修复实验室', exact: true }).click(); await page.getByLabel('搜索 UUID', { exact: true }).fill(search); await page.getByRole('button', { name: '打开搜索', exact: true }).click();
    await expect(page.getByText('存在结果未知试验：预留继续保留', { exact: false })).toBeVisible(); await expect(page.locator('.experiment-report .section-heading')).toContainText('取消中，等待安全结算');
    const reopened = await read(), final = object(reopened.execution), afterReopen = await backend.command('state'), audit = await backend.command('edge_audit');
    expect(final.cancel_requested).toBe(true); expect(object(final.usage).active).toBe(1); expect(final.verification).toBeNull(); expect(objects(final.trials)).toHaveLength(1); expect(afterReopen).toEqual(beforeReopen); expect(afterReopen.source_fingerprint).toBe(seed.source_fingerprint); expect(objects(audit.invocations)).toHaveLength(1);
    const calls = objects(object(audit.indexes).external_calls).filter(row => row.run_id === object(trials[0]!.dispatch).initial_run_id && String(row.provider).startsWith('llm:')); expect(calls).toHaveLength(1); expect(calls[0]!.status).toBe('outcome_unknown'); expect(calls[0]!.result_ref).toBeNull();
    expect(objects(beforeReopen.mutations).filter(row => row.path === `/repair-studies/${studyId}/cancel`)).toHaveLength(1); expect(errors).toEqual([]); expect(await page.locator('vite-error-overlay').count()).toBe(0);
    await writeFile(join(folder, 'unknown.json'), JSON.stringify({ scope: 'real-postgres-api-worker-cas-electron/mock-model/frozen-unknown-injection', runtime, seed, search, studyId, waiting, reopened, beforeReopen, afterReopen, firstAudit, audit, screenshots, errors, root: backend.root }, null, 2)); console.log(`真实确认未知证据：${folder}`);
  } catch (error) {
    await writeFile(join(folder, 'failure.json'), JSON.stringify({ errors, state: await backend.command('state').catch(failure => String(failure)), text: await (await desktop.firstWindow()).locator('body').innerText(), root: backend.root }, null, 2)); console.log(`真实未知失败诊断：${folder}`); throw error;
  } finally { try { await desktop.close(); } finally { await backend.stop(); } }
});
