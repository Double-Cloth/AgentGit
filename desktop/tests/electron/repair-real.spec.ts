import { test, expect, type Page } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, data, objects, startBackend } from '../support/real-client';
import { desktopIdentity, launchDesktop } from '../support/desktop-runtime';

function object(value: unknown): JsonObject { return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}; }

test('真实 Repair Lab：联合搜索、字段缩减、六加四、版本竞争、精确批准和重开', async () => {
  test.setTimeout(600000);
  const backend = await startBackend('repair'), folder = await mkdtemp(join(tmpdir(), 'agentgit-s10-real-'));
  const seed = await backend.command('seed').catch(async failure => { await backend.stop(); throw failure; });
  const profile = join(folder, 'profile'), errors: string[] = [], screenshots: string[] = [];
  let desktop = await launchDesktop(profile, backend.url, TOKEN).catch(async failure => { await backend.stop(); throw failure; });
  function observe(page: Page) { page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (['error', 'warning'].includes(entry.type())) errors.push(entry.text()); }); }
  let search = '';
  try {
    let page = await desktop.firstWindow(); observe(page); const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '修复实验室', exact: true }).click(); await expect(page.getByRole('heading', { name: '修复实验室', exact: true })).toBeVisible();
    await page.getByLabel('失败 Run UUID', { exact: true }).fill(String(seed.failed_run));
    await page.getByLabel('模型决策前起点', { exact: true }).fill(String(seed.base)); await page.getByLabel('screen 验收器摘要', { exact: true }).fill(String(seed.validator));
    await page.getByLabel('有限事件目标 JSON', { exact: false }).fill(JSON.stringify([{ schema_version: 1, event_id: seed.target_event }]));
    const origins = objects(seed.regression);
    for (let i = 0; i < 4; i++) { await page.getByLabel(`回归 ${i + 1} Run UUID`, { exact: true }).fill(String(origins[i]!.source_run_id)); await page.getByLabel(`回归 ${i + 1} 模型决策前起点`, { exact: true }).fill(String(origins[i]!.from_commit)); }
    await page.getByText('完整预算与 Hybrid 条件', { exact: true }).click(); await page.getByLabel('完整修复预算 JSON（Mock 可沿用默认）', { exact: true }).fill(JSON.stringify(seed.budget));
    await page.getByRole('checkbox', { name: '我已核对来源', exact: false }).check(); await page.getByRole('button', { name: '准备并搜索最小修复', exact: true }).dblclick();
    await expect(page.getByRole('button', { name: '开始缩减', exact: true })).toBeEnabled({ timeout: 180000 });
    search = await page.getByLabel('搜索 UUID', { exact: true }).inputValue();
    const screened = data(await page.evaluate(searchId => window.agentgit.getRepairSearch({ searchId }), search));
    expect(objects(screened.trials).some(trial => object(trial.result).verdict === 'failed')).toBe(true);
    expect(object(screened.decision).status).toBe('needs_confirmation');
    await page.getByRole('button', { name: '开始缩减', exact: true }).click();
    await expect(page.getByRole('button', { name: '锁后开启独立复测', exact: true })).toBeEnabled({ timeout: 240000 });
    const workflow = data(await page.evaluate(searchId => window.agentgit.getRepairWorkflow({ searchId }), search));
    const study = data(await page.evaluate(studyId => window.agentgit.getRepairStudy({ studyId }), String(workflow.study_id)));
    const fields = object(study.field_reduction), summary = object(fields.summary);
    expect(summary.status).toBe('complete'); expect(summary.accepted_fields).toBeGreaterThanOrEqual(2);
    expect(summary.atomic_minimality).toBe('one_minimal_under_fixture'); expect(summary.field_minimality).toBe('one_step_minimal_under_fixture');
    const fieldTable = page.getByRole('heading', { name: /字段与必要性复查 ·/ }).locator('..');
    const cells = await fieldTable.locator('tbody tr td:nth-child(3)').allTextContents(); expect(cells.length).toBeGreaterThan(2); expect(cells.every(text => !text.includes('未知'))).toBe(true);
    await page.getByRole('button', { name: '锁后开启独立复测', exact: true }).click();
    await expect(page.getByText('确认 6 / 6 · 回归 4 / 4', { exact: true })).toBeVisible({ timeout: 180000 });
    const confirmed = data(await page.evaluate(studyId => window.agentgit.getRepairValidation({ studyId }), String(workflow.study_id)));
    const execution = object(confirmed.execution), verification = object(execution.verification);
    expect(verification.status).toBe('verified_under_fixture'); expect(objects(execution.trials)).toHaveLength(10);
    const runs = objects(execution.trials).map(row => object(row.result).current_run_id);
    expect(new Set(runs).size).toBe(10); expect(runs).not.toContain(seed.failed_run);
    expect(objects(execution.trials).every(row => Number(object(row.result).fresh_model_calls) > 0)).toBe(true);
    await page.getByRole('button', { name: '生成精确修复建议', exact: true }).click(); await expect(page.getByRole('heading', { name: '精确修复建议', exact: true })).toBeVisible();
    const finalLinks = data(await page.evaluate(searchId => window.agentgit.getRepairWorkflow({ searchId }), search));
    const proposalId = String(finalLinks.proposal_id);
    const proposed = data(await page.evaluate(proposalId => window.agentgit.getRepairProposal({ proposalId }), proposalId));
    const proposal = object(proposed.proposal), before = object(proposal.before), after = object(proposal.after);
    expect(proposal.changed_fields).toEqual(['prompt', 'plan_template']); expect(after.prompt).not.toBe(before.prompt); expect(after.plan_template).not.toBeNull();
    for (const row of objects(execution.trials)) { expect(object(object(row.projection).config).prompt).toBe(after.prompt); expect(object(row.projection).plan).toEqual(after.plan_template); }
    await page.getByRole('button', { name: '核对并获取审阅票据', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeEnabled();
    const bumped = data(await page.evaluate(raw => { const body = JSON.parse(raw) as { config: JsonObject; version: number }; return window.agentgit.createAgent({ config: body.config, expectedVersion: body.version, key: 'native-repair-version-race' }); }, JSON.stringify({ config: before, version: proposed.current_config_version })));
    await page.getByRole('button', { name: '只读刷新', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeDisabled(); await expect(page.getByText('配置版本或建议绑定已变化。', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: '核对并获取审阅票据', exact: true }).click(); await expect(page.getByRole('button', { name: '批准创建配置版本', exact: true })).toBeEnabled();
    const beforeApproval = await backend.command('state');
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0)); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const file = join(folder, `real-${width}.png`); await page.screenshot({ path: file, fullPage: true }); screenshots.push(file); }
    await page.getByRole('button', { name: '批准创建配置版本', exact: true }).dblclick(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible();
    const accepted = data(await page.evaluate(proposalId => window.agentgit.getRepairProposal({ proposalId }), proposalId));
    const decision = object(object(accepted.review).decision); expect(decision.decision).toBe('approve'); expect(decision.execution).toBe('not_started'); expect(decision.new_config_version).toBe(Number(bumped.version) + 1);
    const afterApproval = await backend.command('state'); expect(afterApproval.source_fingerprint).toBe(seed.source_fingerprint);
    const a = object(beforeApproval.counts), b = object(afterApproval.counts); expect(b.jobs).toBe(a.jobs); expect(b.runs).toBe(a.runs); expect(b.agent_configs).toBe(Number(a.agent_configs) + 1); expect(b.proposal_reviews).toBe(Number(a.proposal_reviews) + 1);
    const posts = objects(afterApproval.mutations); expect(posts.filter(row => row.path === '/repair-searches/prepared')).toHaveLength(1); expect(posts.filter(row => row.path === `/repair-proposals/${proposalId}/approve`)).toHaveLength(1);
    const count = posts.length;
    await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); observe(page); await desktopIdentity(desktop, page);
    await page.getByRole('button', { name: '修复实验室', exact: true }).click(); await page.getByLabel('搜索 UUID', { exact: true }).fill(search); await page.getByRole('button', { name: '打开搜索', exact: true }).click(); await expect(page.getByRole('heading', { name: '已记录的决定', exact: true })).toBeVisible();
    expect(objects((await backend.command('mutations')).mutations)).toHaveLength(count);
    const audit = await backend.command('audit', { search }); expect(audit.source_fingerprint).toBe(seed.source_fingerprint);
    expect(errors).toEqual([]); expect(await page.locator('vite-error-overlay').count()).toBe(0);
    await writeFile(join(folder, 'real.json'), JSON.stringify({ scope: 'real-postgres-api-worker-cas-electron/mock-model', runtime, seed, search, workflow, study, confirmed, proposed, accepted, bumped, beforeApproval, afterApproval, audit, screenshots, errors, root: backend.root }, null, 2)); console.log(`S10 真实 Repair Lab 证据：${folder}`);
  } catch (failure) {
    const page = await desktop.firstWindow();
    await writeFile(join(folder, 'failure.json'), JSON.stringify({ search, errors, text: await page.locator('body').innerText(), connection: await page.evaluate(() => window.agentgit.getConnection()), state: await backend.command('state').catch(error => ({ unavailable: String(error) })), root: backend.root }, null, 2)); console.log(`S10 真实联调失败诊断：${folder}`); throw failure;
  } finally { try { await desktop.close(); } finally { await backend.stop(); await expect(backend.command('state')).rejects.toThrow(/联调进程结束|控制通道/); } }
});
