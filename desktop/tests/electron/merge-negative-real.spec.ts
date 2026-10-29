import { test, expect } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, data, objects, startBackend } from '../support/real-client';
import { desktopIdentity, launchDesktop } from '../support/desktop-runtime';

test('真实 Merge Review：错误引文硬阻断、明确拒绝和重开只读', async () => {
  const backend = await startBackend('merge-negative'), folder = await mkdtemp(join(tmpdir(), 'agentgit-s11-rejected-'));
  const seed = await backend.command('seed').catch(async failure => { await backend.stop(); throw failure; });
  const request = seed.request as JsonObject, ours = request.ours as JsonObject, theirs = request.theirs as JsonObject, profile = join(folder, 'profile');
  let desktop = await launchDesktop(profile, backend.url, TOKEN).catch(async failure => { await backend.stop(); throw failure; });
  try {
    let page = await desktop.firstWindow(); const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    const original = await backend.command('merge_state');
    await page.getByRole('button', { name: '合并审阅', exact: true }).click(); await page.getByText('创建合并提案', { exact: true }).click();
    await page.getByLabel('目标 ours分支', { exact: true }).selectOption(String(ours.name)); await page.getByLabel('来源 theirs分支', { exact: true }).selectOption(String(theirs.name)); await page.getByLabel('目标 ours来源 Run UUID', { exact: true }).fill(String(ours.source_run_id)); await page.getByLabel('来源 theirs来源 Run UUID', { exact: true }).fill(String(theirs.source_run_id)); await page.getByLabel('冻结验收器摘要', { exact: true }).fill(String(request.validator_ref));
    await page.getByRole('button', { name: '创建固定来源提案', exact: true }).click(); await expect(page.getByRole('button', { name: '验证当前修订', exact: true })).toBeEnabled();
    const proposalId = await page.getByLabel('提案 UUID', { exact: true }).inputValue(); await page.getByRole('button', { name: '验证当前修订', exact: true }).click(); await expect(page.getByRole('heading', { name: '当前提案 · 存在阻断项', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '核对并获取本人票据', exact: true })).toBeDisabled(); await expect(page.getByRole('button', { name: '批准合并当前候选', exact: true })).toBeDisabled();
    const view = data(await page.evaluate(proposalId => window.agentgit.getMergeProposal({ proposalId }), proposalId)); expect((view.validation as JsonObject).result).toMatchObject({ validated: false });
    await page.getByLabel('拒绝原因', { exact: true }).fill('原文没有支持引文，保留硬拒绝'); await page.getByRole('button', { name: '记录拒绝', exact: true }).click(); await expect(page.getByRole('heading', { name: '当前提案 · 已拒绝', exact: true })).toBeVisible();
    const final = await backend.command('merge_archive', { label: 'rejected' });
    for (const name of ['jobs', 'runs', 'tasks', 'external_calls', 'merge_approvals']) expect((final.counts as JsonObject)[name]).toEqual((original.counts as JsonObject)[name]);
    expect((final.counts as JsonObject).merge_rejections).toBe(1); expect(final.source_fingerprints).toEqual(original.source_fingerprints);
    await page.setViewportSize({ width: 1440, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ path: join(folder, 'rejected.png'), fullPage: true });
    const count = objects((await backend.command('mutations')).mutations).length;
    await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); await desktopIdentity(desktop, page); await page.getByRole('button', { name: '合并审阅', exact: true }).click(); await page.getByRole('button', { name: proposalId.slice(0, 12), exact: true }).click(); await expect(page.getByRole('heading', { name: '当前提案 · 已拒绝', exact: true })).toBeVisible(); await expect(page.getByRole('button', { name: '记录拒绝', exact: true })).toBeDisabled(); expect(objects((await backend.command('mutations')).mutations)).toHaveLength(count);
    await writeFile(join(folder, 'rejected.json'), JSON.stringify({ runtime, seed, proposalId, original, final }, null, 2)); console.log(`真实合并拒绝证据：${folder}`);
  } finally { try { await desktop.close(); } finally { await backend.stop(); } }
});
