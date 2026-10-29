import { test, expect } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, data, objects, startBackend } from '../support/real-client';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const object = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};

test('真实故障候选交接：仅带入核验来源和请求事件，不启动搜索或混用复测样本', async () => {
  const backend = await startBackend('repair'), folder = await mkdtemp(join(tmpdir(), 'agentgit-repair-handoff-'));
  const seed = await backend.command('seed').catch(async error => { await backend.stop(); throw error; });
  const desktop = await launchDesktop(join(folder, 'profile'), backend.url, TOKEN).catch(async error => { await backend.stop(); throw error; });
  const errors: string[] = [], screenshots: string[] = [];
  try {
    const page = await desktop.firstWindow(), runtime = await desktopIdentity(desktop, page);
    page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); });
    await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '差异', exact: true }).click(); await page.getByRole('tab', { name: '故障定位', exact: true }).click();
    await page.getByLabel('失败 Run UUID', { exact: true }).fill(String(seed.failed_run)); await page.getByRole('button', { name: '生成静态报告', exact: true }).click();
    await expect(page.getByRole('heading', { name: '冻结静态报告', exact: true })).toBeVisible();
    const reportId = await page.getByLabel('已有 Debug Report UUID').inputValue();
    const stored = data(await page.evaluate(reportId => window.agentgit.getDebugReport({ reportId }), reportId));
    const candidates = objects(object(object(stored.report).diagnosis).candidates);
    let bound: JsonObject | null = null, index = -1;
    for (let i = 0; i < candidates.length; i++) {
      const reply = await page.evaluate(input => window.agentgit.getDebugCandidateContext(input), { reportId, candidateEventId: Number(object(candidates[i]!.event).event_id), validatorRef: String(seed.validator) });
      if (reply.ok) { bound = reply.data; index = i; break; }
    }
    expect(bound).not.toBeNull();
    await page.locator('.debug-outline > button').nth(index).click();
    await page.getByLabel('候选验收器摘要').fill(String(seed.validator)); await page.getByRole('button', { name: '核对干预上下文', exact: true }).click(); await expect(page.getByText('已核对的调用与检查点', { exact: true })).toBeVisible();
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.getByRole('button', { name: '在修复实验室核对候选', exact: true }).scrollIntoViewIfNeeded(); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const path = join(folder, `candidate-${width}.png`); await captureDesktop(desktop, page, path); screenshots.push(path); }
    const before = await backend.command('state');
    await page.getByRole('button', { name: '在修复实验室核对候选', exact: true }).click(); await expect(page.getByRole('heading', { name: '修复实验室', exact: true })).toBeVisible();
    const context = object(bound!.context), target = object(bound!.request_event);
    await expect(page.getByLabel('失败 Run UUID', { exact: true })).toHaveValue(String(context.source_run_id)); await expect(page.getByLabel('模型决策前起点', { exact: true })).toHaveValue(String(context.base_commit)); await expect(page.getByLabel('screen 验收器摘要', { exact: true })).toHaveValue(String(seed.validator));
    expect(JSON.parse(await page.getByLabel('有限事件目标 JSON', { exact: false }).inputValue())).toEqual([{ schema_version: 1, event_id: target.event_id }]);
    for (let i = 1; i <= 4; i++) { await expect(page.getByLabel(`回归 ${i} Run UUID`, { exact: true })).toHaveValue(''); await expect(page.getByLabel(`回归 ${i} 模型决策前起点`, { exact: true })).toHaveValue(''); }
    await expect(page.getByRole('checkbox', { name: '我已核对来源', exact: false })).not.toBeChecked(); await expect(page.getByRole('button', { name: '准备并搜索最小修复', exact: true })).toBeDisabled();
    const after = await backend.command('state'); expect(after).toEqual(before); expect(after.source_fingerprint).toBe(seed.source_fingerprint);
    expect(objects(after.mutations).map(row => row.path)).toEqual(['/debug-reports']);
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0)); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const path = join(folder, `handoff-${width}.png`); await captureDesktop(desktop, page, path); screenshots.push(path); }
    const confirm = data(await page.evaluate(input => window.agentgit.freezeValidator(input), { sourceRunId: String(context.source_run_id), fromCommit: String(context.base_commit), fixtureSplit: 'confirm' as const, key: 'handoff-confirm-guard' }));
    await page.getByRole('button', { name: '差异', exact: true }).click(); await page.getByRole('tab', { name: '故障定位', exact: true }).click(); await page.locator('.debug-report-list button').first().click(); await page.locator('.debug-outline > button').nth(index).click();
    await page.getByLabel('候选验收器摘要').fill(String(confirm.validator_ref)); await page.getByRole('button', { name: '核对干预上下文', exact: true }).click(); await expect(page.getByText('已核对的调用与检查点', { exact: true })).toBeVisible();
    const beforeGuard = await backend.command('state'); await page.getByRole('button', { name: '在修复实验室核对候选', exact: true }).click(); await expect(page.getByText('修复搜索须沿用已核对的 screen 验收器', { exact: false })).toBeVisible(); await expect(page.getByRole('heading', { name: '冻结静态报告', exact: true })).toBeVisible();
    const afterGuard = await backend.command('state'); expect(afterGuard).toEqual(beforeGuard);
    expect(errors).toEqual([]); expect(await page.locator('vite-error-overlay').count()).toBe(0);
    await writeFile(join(folder, 'handoff.json'), JSON.stringify({ scope: 'real-postgres-api-worker-cas-electron/mock-model', runtime, seed, reportId, stored, bound, before, after, confirmationGuard: { confirm, beforeGuard, afterGuard }, screenshots, errors, root: backend.root }, null, 2)); console.log(`真实候选交接证据：${folder}`);
  } finally { try { await desktop.close(); } finally { await backend.stop(); } }
});
