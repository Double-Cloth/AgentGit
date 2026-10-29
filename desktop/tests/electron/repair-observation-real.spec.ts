import { test, expect, type Page } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, data, objects, startBackend } from '../support/real-client';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const object = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};

test('真实观测补丁六加四：禁止正式批准，仅保存精确实验定义且不排队执行', async () => {
  test.setTimeout(600000);
  const backend = await startBackend('repair-observation'), folder = await mkdtemp(join(tmpdir(), 'agentgit-repair-observation-'));
  const seed = await backend.command('seed').catch(async error => { await backend.stop(); throw error; });
  const profile = join(folder, 'profile'); let desktop = await launchDesktop(profile, backend.url, TOKEN).catch(async error => { await backend.stop(); throw error; });
  const errors: string[] = [], screenshots: string[] = [];
  function observe(page: Page) { page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); }); }
  try {
    let page = await desktop.firstWindow(); observe(page); const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click(); await page.getByRole('button', { name: '修复实验室', exact: true }).click();
    await page.getByLabel('失败 Run UUID', { exact: true }).fill(String(seed.failed_run)); await page.getByLabel('模型决策前起点', { exact: true }).fill(String(seed.base)); await page.getByLabel('screen 验收器摘要', { exact: true }).fill(String(seed.validator));
    await page.getByLabel('有限事件目标 JSON', { exact: false }).fill(JSON.stringify([{ schema_version: 1, event_id: seed.target_event, replacement_call_ids: [seed.replacement_call_id] }]));
    const origins = objects(seed.regression);
    for (let i = 0; i < 4; i++) { await page.getByLabel(`回归 ${i + 1} Run UUID`, { exact: true }).fill(String(origins[i]!.source_run_id)); await page.getByLabel(`回归 ${i + 1} 模型决策前起点`, { exact: true }).fill(String(origins[i]!.from_commit)); }
    await page.getByText('完整预算与 Hybrid 条件', { exact: true }).click(); await page.getByLabel('完整修复预算 JSON（Mock 可沿用默认）', { exact: true }).fill(JSON.stringify(seed.budget)); await page.getByLabel('Hybrid 范围 JSON（可选）', { exact: true }).fill(JSON.stringify(seed.hybrid));
    await page.getByRole('checkbox', { name: '我已核对来源', exact: false }).check(); await page.getByRole('button', { name: '准备并搜索最小修复', exact: true }).click();
    await expect(page.getByRole('button', { name: '开始缩减', exact: true })).toBeEnabled({ timeout: 180000 }); const search = await page.getByLabel('搜索 UUID', { exact: true }).inputValue(); await page.getByRole('button', { name: '开始缩减', exact: true }).click();
    await expect(page.getByRole('button', { name: '锁后开启独立复测', exact: true })).toBeEnabled({ timeout: 120000 }); await page.getByRole('button', { name: '锁后开启独立复测', exact: true }).click();
    await expect(page.getByText('确认 6 / 6 · 回归 4 / 4', { exact: true })).toBeVisible({ timeout: 240000 });
    const links = data(await page.evaluate(searchId => window.agentgit.getRepairWorkflow({ searchId }), search)), studyId = String(links.study_id);
    const validation = data(await page.evaluate(studyId => window.agentgit.getRepairValidation({ studyId }), studyId)), execution = object(validation.execution), verification = object(execution.verification);
    expect(verification.status).toBe('verified_under_fixture'); expect(verification.has_observation_patch).toBe(true); expect(verification.production_eligible).toBe(false);
    const trials = objects(execution.trials); expect(trials).toHaveLength(10); expect(new Set(trials.map(row => object(row.result).current_run_id)).size).toBe(10); expect(trials.every(row => object(row.result).test_only === true && Number(object(row.result).fresh_model_calls) > 0)).toBe(true);
    await page.getByRole('button', { name: '生成精确修复建议', exact: true }).click(); await expect(page.getByText('包含工具观测替换：test_only', { exact: false })).toBeVisible();
    const finalLinks = data(await page.evaluate(searchId => window.agentgit.getRepairWorkflow({ searchId }), search)), proposalId = String(finalLinks.proposal_id);
    const proposed = data(await page.evaluate(proposalId => window.agentgit.getRepairProposal({ proposalId }), proposalId)), proposal = object(proposed.proposal); expect(proposal.after).toBeNull(); expect(proposal.adoption_scope).toBe('save_experiment_only');
    await page.getByRole('button', { name: '核对并获取审阅票据', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeDisabled(); await expect(page.getByRole('button', { name: '仅保存为实验', exact: true })).toBeEnabled();
    const beforeNegative = await backend.command('state');
    const ticket = data(await page.evaluate(proposalId => window.agentgit.beginRepairReview({ proposalId, key: 'observation-negative-ticket' }), proposalId));
    const rejected = await page.evaluate(input => window.agentgit.decideRepairReview(input), { proposalId, reviewId: String(ticket.review_id), proposalRef: String(proposed.proposal_ref), expectedConfigVersion: Number(object(ticket.ticket).expected_config_version), decision: 'approve' as const, key: 'observation-forbidden-approve' }); expect(rejected.ok).toBe(false); if (!rejected.ok) expect(rejected.error.code).toBe('forbidden');
    const beforeSave = await backend.command('state'); expect(beforeSave.counts).toEqual(beforeNegative.counts); expect(beforeSave.source_fingerprint).toBe(seed.source_fingerprint);
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.getByText('包含工具观测替换：test_only', { exact: false }).scrollIntoViewIfNeeded(); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const path = join(folder, `observation-${width}.png`); await captureDesktop(desktop, page, path); screenshots.push(path); }
    await page.getByRole('button', { name: '仅保存为实验', exact: true }).dblclick(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible();
    const accepted = data(await page.evaluate(proposalId => window.agentgit.getRepairProposal({ proposalId }), proposalId)), review = object(accepted.review), decision = object(review.decision);
    expect(decision.decision).toBe('save_experiment'); expect(decision.execution).toBe('not_started'); expect(decision.new_config_id).toBeNull(); expect(object(object(review.saved_experiment).definition).patches).toEqual(proposal.patches);
    const afterSave = await backend.command('state'), a = object(beforeSave.counts), b = object(afterSave.counts); expect(b.jobs).toBe(a.jobs); expect(b.runs).toBe(a.runs); expect(b.agent_configs).toBe(a.agent_configs); expect(b.proposal_reviews).toBe(Number(a.proposal_reviews) + 1); expect(afterSave.source_fingerprint).toBe(seed.source_fingerprint);
    const posts = objects(afterSave.mutations); expect(posts.filter(row => row.path === `/repair-proposals/${proposalId}/approve`)).toHaveLength(2); expect(posts.filter(row => row.path === `/repair-proposals/${proposalId}/approve`).map(row => row.status)).toEqual([403, 200]);
    await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); observe(page); await desktopIdentity(desktop, page); await page.getByRole('button', { name: '修复实验室', exact: true }).click(); await page.getByLabel('搜索 UUID', { exact: true }).fill(search); await page.getByRole('button', { name: '打开搜索', exact: true }).click(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible();
    expect(await backend.command('state')).toEqual(afterSave); const audit = await backend.command('audit', { search }); expect(audit.source_fingerprint).toBe(seed.source_fingerprint); expect(errors).toEqual([]); expect(await page.locator('vite-error-overlay').count()).toBe(0);
    await writeFile(join(folder, 'observation.json'), JSON.stringify({ scope: 'real-postgres-api-worker-cas-electron/mock-model/observation-test-only', runtime, seed, search, studyId, validation, proposed, accepted, ticket, rejected, beforeNegative, beforeSave, afterSave, audit, screenshots, errors, root: backend.root }, null, 2)); console.log(`真实观测禁采纳证据：${folder}`);
  } catch (error) {
    await writeFile(join(folder, 'failure.json'), JSON.stringify({ errors, state: await backend.command('state').catch(failure => String(failure)), text: await (await desktop.firstWindow()).locator('body').innerText(), root: backend.root }, null, 2)); console.log(`真实观测失败诊断：${folder}`); throw error;
  } finally { try { await desktop.close(); } finally { await backend.stop(); } }
});
