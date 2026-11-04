import { test, expect, type Page } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, data, objects, startBackend, completed } from '../support/real-client';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const object = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};
test('真实 Merge Review：三方来源、完整修订、双父批准、独立初始化与显式 Recorded', async () => {
  test.setTimeout(360000);
  const backend = await startBackend('merge'), folder = await mkdtemp(join(tmpdir(), 'agentgit-s11-real-'));
  const seed = await backend.command('seed').catch(async failure => { await backend.stop(); throw failure; });
  const request = object(seed.request), ours = object(request.ours), theirs = object(request.theirs), profile = join(folder, 'profile');
  let desktop = await launchDesktop(profile, backend.url, TOKEN).catch(async failure => { await backend.stop(); throw failure; });
  let page: Page | undefined;
  const screenshots: string[] = [], errors: string[] = [];
  const observe = (value: Page) => { value.on('pageerror', error => errors.push(error.message)); value.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); }); };
  try {
    page = await desktop.firstWindow(); observe(page); const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    const original = await backend.command('merge_state');
    await page.getByRole('button', { name: '合并审阅', exact: true }).click(); await page.getByText('创建合并提案', { exact: true }).click();
    await page.getByLabel('目标 ours分支', { exact: true }).selectOption(String(ours.name)); await page.getByLabel('来源 theirs分支', { exact: true }).selectOption(String(theirs.name));
    await page.getByLabel('目标 ours来源 Run UUID', { exact: true }).fill(String(ours.source_run_id)); await page.getByLabel('来源 theirs来源 Run UUID', { exact: true }).fill(String(theirs.source_run_id)); await page.getByLabel('冻结验收器摘要', { exact: true }).fill(String(request.validator_ref));
    await page.getByRole('button', { name: '创建固定来源提案', exact: true }).dblclick(); await expect(page.getByRole('heading', { name: '当前提案 · 候选待验证', exact: true })).toBeVisible();
    const proposalId = await page.getByLabel('提案 UUID', { exact: true }).inputValue();
    const initial = data(await page.evaluate(proposalId => window.agentgit.getMergeProposal({ proposalId }), proposalId));
    await page.getByRole('button', { name: '读取 report.md 原文', exact: true }).first().click(); await expect(page.locator('.merge-origins pre').filter({ hasText: '## ' }).first()).toBeVisible();
    const reportText = String(objects(object(initial.candidate).artifacts).find(file => file.path === 'report.md')!.text);
    await page.getByLabel('report.md 编辑方式', { exact: true }).selectOption('write'); await page.getByLabel('report.md 完整文本', { exact: true }).fill(reportText);
    await page.getByRole('button', { name: '保存为新修订', exact: true }).click();
    await expect.poll(async () => object(data(await page!.evaluate(proposalId => window.agentgit.getMergeProposal({ proposalId }), proposalId)).revision).revision).toBe(2);
    await expect(page.getByRole('button', { name: '验证当前修订', exact: true })).toBeEnabled(); await page.getByRole('button', { name: '验证当前修订', exact: true }).click(); await expect(page.getByRole('button', { name: '核对并获取本人票据', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '核对并获取本人票据', exact: true }).click(); await page.getByRole('checkbox', { name: '我已核对三方来源', exact: false }).check(); await expect(page.getByRole('button', { name: '批准合并当前候选', exact: true })).toBeEnabled();
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0)); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const file = join(folder, `review-${width}.png`); await captureDesktop(desktop, page, file, true); screenshots.push(file);
      const top = join(folder, `review-${width}-top.png`); await captureDesktop(desktop, page, top); screenshots.push(top);
      for (const title of ['三方原文与来源', '验证与决定']) { const detail = join(folder, `review-${width}-${title}.png`); await page.locator('article > section').filter({ has: page.getByRole('heading', { name: title, exact: true }) }).evaluate(element => element.scrollIntoView({ block: 'start' })); await captureDesktop(desktop, page, detail); screenshots.push(detail); }
    }
    await page.getByRole('button', { name: '批准合并当前候选', exact: true }).dblclick(); await expect(page.getByRole('heading', { name: '已合并 · 后续执行须新建任务', exact: true })).toBeVisible();
    const approved = data(await page.evaluate(proposalId => window.agentgit.getMergeProposal({ proposalId }), proposalId)), approval = object(approved.approval);
    const commit = data(await page.evaluate(commitId => window.agentgit.getCommit({ commitId }), String(approval.commit_id)));
    expect(object(commit.manifest).parents).toEqual([ours.head, theirs.head]);
    const beforeRun = await backend.command('merge_archive', { label: 'approved' });
    for (const name of ['jobs', 'runs', 'tasks', 'external_calls', 'agent_configs']) expect(object(beforeRun.counts)[name]).toEqual(object(original.counts)[name]);
    expect(object(beforeRun.counts).merge_approvals).toBe(1); expect(beforeRun.source_fingerprints).toEqual(original.source_fingerprints);
    await backend.command('merge_gate', { enabled: true });
    await page.getByLabel('新分支名称', { exact: true }).fill('merged/native'); await page.getByRole('button', { name: '建立独立合并分支', exact: true }).click();
    await page.getByLabel('Agent 配置 UUID', { exact: true }).fill(String(seed.config_id)); await page.getByLabel('配置版本', { exact: true }).fill(String(seed.config_version)); await page.getByLabel('独立预算 JSON', { exact: true }).fill(JSON.stringify(seed.budget)); await page.getByRole('checkbox', { name: '我选择此配置和预算', exact: false }).check(); await page.getByRole('button', { name: '提交独立新任务', exact: true }).dblclick(); await expect(page.getByRole('heading', { name: '新任务已提交', exact: true })).toBeVisible();
    const run = objects(data(await page.evaluate(() => window.agentgit.listRuns({ limit: 100 }))).items).find(item => item.branch_name === 'merged/native')!;
    expect(run).toBeTruthy(); const newRun = String(run.id);
    const queued = await backend.command('merge_state'); expect(object(queued.counts).external_calls).toEqual(object(original.counts).external_calls); expect(Number(object(queued.counts).runs)).toBe(Number(object(original.counts).runs) + 1);
    const input = await backend.command('merge_run', { run: newRun }), state = object(object(input.initial).data);
    expect(state.completed).toEqual([]); expect(state.messages).toEqual([]); expect(state.pending_tool).toBeNull(); expect(object(state.usage).model_calls).toBe(0); expect(object(state.usage).tool_calls).toBe(0);
    await backend.command('merge_gate', { enabled: false }); await completed(page, newRun);
    const actual = await backend.command('merge_run', { run: newRun }), checkpoints = actual.checkpoints as string[]; expect(checkpoints.length).toBeGreaterThan(1);
    const invalid = await page.evaluate(input => window.agentgit.replay({ request: { schema_version: 1, mode: 'recorded', source_run_id: input.run, from_commit: input.merge, until_commit: input.last, branch_name: 'merge-root-invalid', expected_version: 0 }, key: 'native-merge-root-rejected' }), { run: newRun, merge: String(approval.commit_id), last: checkpoints.at(-1)! });
    expect(invalid.ok).toBe(false); if (!invalid.ok) expect(invalid.error.code).toBe('recording_missing');
    await page.getByRole('button', { name: '查看新 Run 时间线', exact: true }).click(); await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    await page.getByRole('combobox').selectOption('ours'); await expect(page.locator(`.react-flow__edge[data-id$=":${String(approval.commit_id)}"]`)).toHaveCount(2);
    await page.setViewportSize({ width: 1440, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0)); const graph = join(folder, 'two-parent-history.png'); await captureDesktop(desktop, page, graph, true); screenshots.push(graph);
    await page.getByRole('button', { name: '从所选检查点分支 →', exact: true }).click(); await page.getByLabel('来源 Run', { exact: false }).fill(newRun); await page.getByLabel('安全起点 Commit', { exact: true }).fill(checkpoints[0]!); await page.getByLabel('验证终点 Commit', { exact: true }).fill(checkpoints.at(-1)!); await page.getByLabel('新分支名称', { exact: true }).fill('recorded/native-merge');
    const beforeRecorded = await backend.command('merge_state'); await page.getByRole('button', { name: '提交 Recorded', exact: true }).click(); await expect(page.getByRole('button', { name: '打开新运行时间线 →', exact: true })).toBeVisible();
    const recorded = objects(data(await page.evaluate(() => window.agentgit.listRuns({ limit: 100 }))).items).find(item => item.branch_name === 'recorded/native-merge')!; await completed(page, String(recorded.id));
    const final = await backend.command('merge_archive', { label: 'final' }); expect(object(final.counts).external_calls).toEqual(object(beforeRecorded.counts).external_calls); expect(final.source_fingerprints).toEqual(original.source_fingerprints); expect(object(final.raw_audit).merged_jobs).toBe(1); expect(object(final.raw_audit).recorded_jobs).toBe(1);
    const count = objects((await backend.command('mutations')).mutations).length;
    await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); observe(page); await desktopIdentity(desktop, page); await page.getByRole('button', { name: '合并审阅', exact: true }).click(); await page.getByRole('button', { name: proposalId.slice(0, 12), exact: true }).click(); await expect(page.getByRole('heading', { name: '已合并 · 后续执行须新建任务', exact: true })).toBeVisible(); expect(objects((await backend.command('mutations')).mutations)).toHaveLength(count); await expect(page.getByRole('button', { name: '批准合并当前候选', exact: true })).toBeDisabled();
    expect(errors).toEqual([]); await writeFile(join(folder, 'native-merge.json'), JSON.stringify({ runtime, seed, proposalId, newRun, recorded, original, beforeRun, queued, input, final, screenshots, errors }, null, 2)); console.log(`真实合并桌面证据：${folder}`);
  } catch (failure) {
    if (page && !page.isClosed()) { await captureDesktop(desktop, page, join(folder, 'failure.png'), true); await writeFile(join(folder, 'failure.txt'), await page.locator('body').innerText()); }
    console.log(`真实合并失败证据：${folder}`); throw failure;
  } finally { try { await desktop.close(); } finally { await backend.stop(); } }
});
