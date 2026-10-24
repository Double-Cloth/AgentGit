import { test, expect, type Page } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, completed, data, objects, startBackend } from '../support/real-client';
import { desktopIdentity, launchDesktop } from '../support/desktop-runtime';

function object(value: unknown): JsonObject { return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}; }

test('真实 Electron/API/Worker：失败 Run 生成报告、候选干预效果与精确证据跳转', async () => {
  test.setTimeout(300000);
  const backend = await startBackend(), evidence = await mkdtemp(join(tmpdir(), 'agentgit-s8-real-'));
  const profile = join(evidence, 'profile'), errors: string[] = [], screenshots: string[] = [], transientReads: JsonObject[] = [];
  let desktop = await launchDesktop(profile, backend.url, TOKEN).catch(async error => { await backend.stop(); throw error; });
  let reportId = '';
  function observe(page: Page) { page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); }); }
  try {
    let page = await desktop.firstWindow(); observe(page); const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('新项目名称').fill('S8 报告与候选真实联调'); await page.getByRole('button', { name: '创建并选择', exact: true }).click(); await expect(page.locator('.contextbar code')).toHaveText(/^[0-9a-f-]{36}$/); await page.getByRole('button', { name: '收起', exact: true }).click();
    const capability = data(await page.evaluate(() => window.agentgit.capabilities()));
    const config = data(await page.evaluate(raw => window.agentgit.createAgent({ config: JSON.parse(raw) as JsonObject, expectedVersion: 0, key: 's8-config' }), JSON.stringify(object(capability.default_config))));
    const task = data(await page.evaluate(configId => window.agentgit.createTask({ configId, configVersion: 1, goal: { schema_version: 1, text: '核验三个冻结语料框架的报告与引用' }, key: 's8-task' }), String(config.config_id)));
    const submitted = data(await page.evaluate(taskId => window.agentgit.submitRun({ taskId, configVersion: 1, expectedBranchVersion: 0, expectedHead: null, branchName: 'main', key: 's8-source' }), String(task.task_id)));
    const source = String(submitted.run_id); await completed(page, source);
    const repo = String(data(await page.evaluate(() => window.agentgit.getConnection())).repositoryId);
    const commits = objects(data(await page.evaluate(() => window.agentgit.listCommits({ branchName: 'main', limit: 100 }))).items), base = String(commits.at(-1)!.commit_id), head = String(commits[0]!.commit_id);
    const checkout = data(await page.evaluate(commitId => window.agentgit.getCommit({ commitId }), head));
    const goodPlan = object(object(object(checkout.snapshot).data).plan), badPlan = { ...goodPlan, nodes: objects(goodPlan.nodes).filter(node => node.key !== 'write-sources') };
    const frozen = data(await page.evaluate(scope => window.agentgit.freezeValidator({ sourceRunId: scope.source, fromCommit: scope.base, key: 's8-validator' }), { source, base }));
    const construction = data(await page.evaluate(raw => window.agentgit.createExperiment({ request: JSON.parse(raw) as JsonObject, key: 's8-construct' }), JSON.stringify({ schema_version: 1, source_run_id: source, from_commit: base, validator_ref: frozen.validator_ref!, repeats: 1, budget: object(object(capability.default_config).limits), groups: [{ key: 'A', label: '正常参考' }, { key: 'B', label: '省略来源文件', plan: badPlan }] })));
    const readExperiment = async (id: string) => {
      const reply = await page.evaluate(experimentId => window.agentgit.getExperiment({ experimentId }), id);
      if (reply.ok) return reply.data;
      if (!['connection_required', 'connection_timeout', 'connection_unavailable', 'connection_lost', 'database_unavailable', 'context_changed', 'request_cancelled', 'busy', 'version_conflict'].includes(reply.error.code)) throw new Error(`${reply.error.code}: ${reply.error.message}`);
      const state = await page.evaluate(() => window.agentgit.getConnection());
      transientReads.push({ code: reply.error.code, message: reply.error.message, state: state as unknown as JsonObject });
      return { trials: [] } as JsonObject;
    };
    let constructed: JsonObject = {};
    await expect.poll(async () => { constructed = await readExperiment(String(construction.experiment_id)); return objects(constructed.trials).filter(item => item.verdict !== 'incomplete').length; }, { timeout: 120000 }).toBe(2);
    const failed = objects(constructed.trials).find(item => item.group === 'B')!; expect(failed.verdict).toBe('failed');
    const before = await backend.command('storage_audit', { repository: repo, source_run: source });
    await page.getByRole('button', { name: '差异', exact: true }).click(); await page.getByRole('tab', { name: '故障定位', exact: true }).click();
    await page.getByLabel('失败 Run UUID', { exact: true }).fill(String(failed.run_id)); await page.getByLabel('成功参考 Run UUID（可选）').fill(source); await page.getByRole('button', { name: '生成静态报告', exact: true }).click();
    await expect(page.getByRole('heading', { name: '冻结静态报告', exact: true })).toBeVisible(); reportId = await page.getByLabel('已有 Debug Report UUID').inputValue();
    const stored = data(await page.evaluate(reportId => window.agentgit.getDebugReport({ reportId }), reportId));
    const candidate = objects(object(object(stored.report).diagnosis).candidates)[0]!, candidateEventId = Number(object(candidate.event).event_id);
    await expect(page.getByRole('navigation', { name: '故障候选' }).getByRole('button').first()).toContainText('write-report');
    await expect(page.getByText('missing_artifacts', { exact: true })).toBeVisible();
    await page.getByLabel('候选验收器摘要').fill(String(frozen.validator_ref)); await page.getByRole('button', { name: '核对干预上下文', exact: true }).click(); await expect(page.getByText('已核对的调用与检查点', { exact: true })).toBeVisible();
    const bound = data(await page.evaluate(input => window.agentgit.getDebugCandidateContext(input), { reportId, candidateEventId, validatorRef: String(frozen.validator_ref) })), context = object(bound.context);
    await page.getByLabel('候选完整计划 JSON').fill(JSON.stringify(goodPlan)); await page.getByLabel('候选干预理由').fill('补回未完成的 write-sources，保持固定验收输入。');
    await page.getByRole('button', { name: '保存候选干预', exact: true }).click(); await expect(page.getByLabel('已创建候选干预 ID')).toBeVisible(); const interventionId = await page.getByLabel('已创建候选干预 ID').inputValue();
    await backend.command('worker_gate', { enabled: true });
    await page.getByRole('button', { name: '前往 A/B 表单', exact: true }).click(); await expect(page.getByLabel('B 组干预 ID')).toHaveValue(interventionId); await expect(page.getByLabel('实验来源 Run', { exact: true })).toHaveValue(String(failed.run_id)); await expect(page.getByLabel('实验安全起点 Commit')).toHaveValue(String(context.base_commit));
    await expect(page.getByText('来源状态', { exact: true })).toBeVisible();
    const formValidity = await page.getByRole('button', { name: '提交 A/B 实验', exact: true }).evaluate(button => { const form = (button as HTMLButtonElement).form; return { form: Boolean(form), valid: form?.checkValidity(), invalid: [...(form?.querySelectorAll('input,select,textarea') ?? [])].map(element => element as HTMLInputElement).filter(element => !element.validity.valid).map(element => ({ type: element.type, value: element.value, message: element.validationMessage })) }; });
    expect(formValidity, JSON.stringify(formValidity)).toMatchObject({ form: true, valid: true });
    await expect(page.getByRole('button', { name: '提交 A/B 实验', exact: true })).toBeEnabled(); await page.getByRole('button', { name: '提交 A/B 实验', exact: true }).click(); await expect(page.getByRole('heading', { name: '独立试验', exact: true })).toBeVisible();
    const experiment = { experiment_id: await page.getByLabel('Experiment UUID', { exact: true }).inputValue() };
    await page.getByRole('button', { name: '差异', exact: true }).click(); await page.getByRole('tab', { name: '故障定位', exact: true }).click(); await page.locator('.debug-report-list button').first().click();
    await page.getByLabel('候选 Experiment UUID').fill(String(experiment.experiment_id)); await page.getByRole('button', { name: '核对实验效果', exact: true }).click(); await expect(page.getByText('试验尚未完整判定 · 改善未知', { exact: true })).toBeVisible();
    await backend.command('worker_gate', { enabled: false });
    await expect.poll(async () => objects((await readExperiment(String(experiment.experiment_id))).trials).filter(item => item.verdict !== 'incomplete').length, { timeout: 180000 }).toBe(10);
    await page.getByRole('button', { name: '核对实验效果', exact: true }).click(); await expect(page.getByText('两组均已判定 · 成功率差 1', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: /A 组 · 通过 0/ })).toBeVisible(); await expect(page.getByRole('heading', { name: /B 组 · 通过 5/ })).toBeVisible();
    const effect = data(await page.evaluate(input => window.agentgit.getDebugCandidateExperiment(input), { reportId, candidateEventId, experimentId: String(experiment.experiment_id) }));
    expect(objects(object(effect.experiment).trials)).toHaveLength(10); expect(data(await page.evaluate(reportId => window.agentgit.getDebugReport({ reportId }), reportId))).toEqual(stored);
    for (const width of [1440, 1024, 768, 360]) {
      await page.setViewportSize({ width, height: 1000 }); await page.getByRole('heading', { name: '候选决策证据', exact: true }).scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      const clippedCandidates = await page.locator('.debug-outline > button').evaluateAll(buttons => buttons.filter(button => button.scrollHeight > button.clientHeight + 1).map(button => ({ text: button.textContent, height: button.clientHeight, scrollHeight: button.scrollHeight })));
      expect(clippedCandidates, `候选按钮在 ${width}px 不得压缩裁字`).toEqual([]);
      const path = join(evidence, `candidate-${width}.png`); await page.screenshot({ path }); screenshots.push(path);
    }
    const ref = object(candidate.event); await page.getByRole('button', { name: `事件 ${String(ref.seq)} · ID ${String(ref.event_id)}`, exact: true }).first().click(); await expect(page.getByRole('heading', { name: `定位证据 · 事件 ${String(ref.seq)}`, exact: true })).toBeFocused();
    await expect(page.locator('.focused-event')).toContainText(String(ref.event_id));
    const mutations = await backend.command('mutation_audit'); expect(objects(mutations.mutations).filter(item => item.path === '/debug-reports')).toHaveLength(1);
    const after = await backend.command('storage_audit', { repository: repo, source_run: source }); expect(after.source_fingerprint).toBe(before.source_fingerprint);
    await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); observe(page); await desktopIdentity(desktop, page);
    await page.getByRole('button', { name: '差异', exact: true }).click(); await page.getByRole('tab', { name: '故障定位', exact: true }).click(); await page.locator('.debug-report-list button').first().click(); await expect(page.getByRole('heading', { name: '冻结静态报告', exact: true })).toBeVisible();
    expect(objects((await backend.command('mutation_audit')).mutations).filter(item => item.path === '/debug-reports')).toHaveLength(1); expect(errors).toEqual([]);
    await writeFile(join(evidence, 'real.json'), JSON.stringify({ runtime, repo, source, failed: failed.run_id, reportId, stored, effect, before, after, mutations, screenshots, errors, transientReads, responseAudit: await backend.command('response_audit'), root: backend.root }, null, 2)); console.log(`S8 真实联调证据：${evidence}`);
  } catch (error) {
    const page = await desktop.firstWindow();
    await writeFile(join(evidence, 'failure.json'), JSON.stringify({ reportId, errors, transientReads, connection: await page.evaluate(() => window.agentgit.getConnection()), responseAudit: await backend.command('response_audit'), text: await page.locator('body').innerText(), invalid: await page.locator(':invalid').evaluateAll(elements => elements.map(element => ({ tag: element.tagName, text: element.textContent?.slice(0, 100), value: (element as HTMLInputElement).value, message: (element as HTMLInputElement).validationMessage }))) }, null, 2));
    console.log(`S8 失败诊断：${evidence}`); throw error;
  } finally { try { await desktop.close(); } finally { await backend.stop(); } }
});
