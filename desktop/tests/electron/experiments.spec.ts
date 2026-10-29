import { test, expect, type Page } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, completed, data, objects, startBackend } from '../support/real-client';
import { desktopIdentity, EVIDENCE, launchDesktop } from '../support/desktop-runtime';

test('真实 Electron/API/Worker：十 trial、原键重试、当前版本控制、Hybrid 合成来源与重开只读', async () => {
  const backend = await startBackend(), evidence = await mkdtemp(join(tmpdir(), 'agentgit-s7-desktop-'));
  const profile = join(evidence, 'profile'), errors: string[] = [];
  const launch = () => launchDesktop(profile, backend.url, TOKEN);
  let desktop = await launch().catch(async error => { await backend.stop(); throw error; });
  let latestId = '';
  function observe(page: Page) { page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); }); }
  try {
    let page = await desktop.firstWindow(); observe(page);
    const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('新项目名称').fill('S7 桌面实验真实联调'); await page.getByRole('button', { name: '创建并选择', exact: true }).click();
    await page.getByRole('button', { name: '收起', exact: true }).click(); await page.getByRole('button', { name: '新建运行', exact: true }).click();
    await page.getByRole('button', { name: '提交运行', exact: true }).click();
    await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    const source = (await page.locator('.page-heading code').innerText()).trim(); await completed(page, source);
    const repo = String(data(await page.evaluate(() => window.agentgit.getConnection())).repositoryId);
    const baseline = await backend.command('audit', { repository: repo, source_run: source }); expect(objects(baseline.source_tools)).toHaveLength(5);
    const commits = objects(data(await page.evaluate(() => window.agentgit.listCommits({ branchName: 'main', limit: 100 }))).items);
    const base = String(commits.at(-1)!.commit_id), end = String(commits[0]!.commit_id);
    const read = async (id: string) => data(await page.evaluate(experimentId => window.agentgit.getExperiment({ experimentId }), id));
    await page.getByRole('button', { name: '从所选检查点分支 →' }).click(); await page.getByRole('button', { name: '受控 A/B 实验', exact: true }).click();
    await page.getByLabel('实验安全起点 Commit').fill(base); await page.getByRole('button', { name: '冻结共同验收器', exact: true }).click();
    await expect(page.getByText('查看验收规则与固定输入', { exact: true })).toBeVisible();
    await expect(page.getByLabel('每组重复次数')).toHaveValue('5');
    await backend.command('worker_gate', { enabled: true }); await backend.command('lose_experiment_reply');
    await page.getByRole('button', { name: '提交 A/B 实验', exact: true }).click();
    await expect(page.getByText('测试丢失已提交的实验响应', { exact: false })).toBeVisible();
    await expect(page.getByRole('button', { name: '开始另一实验', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '重新选择来源', exact: true })).toBeDisabled();
    await page.getByRole('button', { name: '单次分支', exact: true }).click(); await page.getByRole('button', { name: '受控 A/B 实验', exact: true }).click();
    await page.getByRole('button', { name: '用原键继续提交', exact: true }).dblclick();
    await expect(page.getByRole('heading', { name: '独立试验', exact: true })).toBeVisible();
    const liveId = await page.getByLabel('Experiment UUID', { exact: true }).inputValue(), queued = await read(liveId); latestId = liveId;
    expect(objects(queued.trials)).toHaveLength(10); expect(new Set(objects(queued.trials).map(item => item.run_id)).size).toBe(10);
    const posted = objects((await backend.command('mutation_audit')).mutations).filter(item => item.path === '/experiments'); expect(posted).toHaveLength(2); expect(posted[0]!.key).toBe(posted[1]!.key);
    await page.getByRole('button', { name: '暂停 A · 1', exact: true }).click();
    await expect(page.getByText('已请求暂停，等待 Worker 安全点与真实状态刷新。', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '取消 B · 5', exact: true }).click();
    await backend.command('worker_gate', { enabled: false });
    await expect(page.getByRole('button', { name: '恢复 A · 1', exact: true })).toBeEnabled({ timeout: 180000 }); await page.getByRole('button', { name: '恢复 A · 1', exact: true }).click();
    await expect(page.getByRole('heading', { name: /A 组 · 通过 5/ })).toBeVisible({ timeout: 300000 });
    await expect(page.getByRole('heading', { name: /B 组 · 通过 4/ })).toBeVisible({ timeout: 300000 });
    const live = await read(liveId); expect(objects(live.trials).filter(item => item.verdict === 'passed')).toHaveLength(9); expect(objects(live.trials).filter(item => item.status === 'cancelled')).toHaveLength(1);
    await page.getByRole('button', { name: '开始另一实验', exact: true }).click(); await page.getByLabel('实验执行模式').selectOption('hybrid'); await page.getByLabel('Hybrid 录制范围终点').fill(end);
    const events = objects(data(await page.evaluate(runId => window.agentgit.getEvents({ runId, limit: 500 }), source)).items);
    const target = events.find(item => item.type === 'tool.started' && String(item.logical_step_id).includes('source'));
    // 事件正文只在上下文返回；依次核对工具调用，避免依赖逻辑步骤命名。
    let selected: JsonObject | undefined;
    for (const event of target ? [target, ...events.filter(item => item !== target)] : events) {
      if (event.type !== 'tool.started') continue;
      const reply = await page.evaluate(input => window.agentgit.getInterventionContext(input), { sourceRunId: source, fromCommit: base, targetEventId: Number(event.id), validatorRef: String((live.manifest as JsonObject).request && ((live.manifest as JsonObject).request as JsonObject).validator_ref) });
      if (reply.ok && ((reply.data.precondition as JsonObject).occurrence as JsonObject).provider === 'tool:source_read/v1') { selected = reply.data; break; }
    }
    expect(selected).toBeDefined();
    await page.getByLabel('目标事件 ID', { exact: true }).fill(String(selected!.target_event_id)); await page.getByRole('button', { name: '读取调用上下文', exact: true }).click();
    await expect(page.getByRole('button', { name: '加入 Hybrid 只读调用', exact: true })).toBeEnabled(); await page.getByRole('button', { name: '加入 Hybrid 只读调用', exact: true }).click();
    await page.getByText('高级：冻结 test_only 工具响应补丁', { exact: true }).click(); await page.getByLabel('替换调用 UUID').fill(String(selected!.source_call_id));
    await page.getByRole('button', { name: '冻结工具响应补丁', exact: true }).click(); await expect(page.getByRole('button', { name: '加入 B 组干预', exact: true })).toBeVisible(); await page.getByRole('button', { name: '加入 B 组干预', exact: true }).click();
    await expect(page.getByLabel('B 组条件类型')).toHaveValue('interventions'); await page.getByRole('button', { name: '提交 A/B 实验', exact: true }).click();
    await expect(page.getByLabel('Experiment UUID', { exact: true })).not.toHaveValue(liveId); const hybridId = await page.getByLabel('Experiment UUID', { exact: true }).inputValue();
    latestId = hybridId;
    await expect(page.getByRole('heading', { name: /A 组 · 通过 5/ })).toBeVisible({ timeout: 300000 });
    await expect(page.getByRole('heading', { name: /B 组 · 通过 5/ })).toBeVisible({ timeout: 300000 });
    const hybrid = await read(hybridId); expect(hybrid.test_only).toBe(true);
    for (const trial of objects(hybrid.trials)) expect((trial.replay as JsonObject).actual).toMatchObject(trial.group === 'B' ? { synthetic_observations: 1 } : { recorded_observations: 1 });
    await page.getByRole('button', { name: '刷新试验状态', exact: true }).click(); await expect(page.getByRole('heading', { name: /A 组 · 通过 5/ })).toBeVisible();
    const screenshots: string[] = [];
    for (const width of [1440, 1024, 768, 360]) {
      await page.setViewportSize({ width, height: 1000 }); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      const layout = await page.evaluate(() => ({ width: innerWidth, height: document.documentElement.scrollHeight, fonts: document.fonts.status, visibility: document.visibilityState }));
      await writeFile(join(evidence, `capture-${width}.json`), JSON.stringify(layout));
      const path = join(evidence, `experiments-${width}.png`); await page.screenshot({ path, fullPage: true, animations: 'disabled', timeout: 60000 }); screenshots.push(path);
    }
    const before = await backend.command('traffic'); await desktop.close(); desktop = await launch(); page = await desktop.firstWindow(); observe(page);
    await desktopIdentity(desktop, page);
    await page.getByRole('button', { name: '分支实验', exact: true }).click(); await page.getByRole('button', { name: '受控 A/B 实验', exact: true }).click();
    await page.getByLabel('Experiment UUID', { exact: true }).fill(hybridId); await page.getByRole('button', { name: '只读查询实验', exact: true }).click(); await expect(page.getByRole('heading', { name: '独立试验', exact: true })).toBeVisible();
    expect(await backend.command('traffic')).toEqual(before); expect(errors).toEqual([]);
    const final = await backend.command('audit', { repository: repo, source_run: source }); expect(final.source_fingerprint).toBe(baseline.source_fingerprint); expect(final.remaining_execution_containers).toEqual([]);
    await writeFile(`../.agentgit/evidence/${EVIDENCE}-real.json`, JSON.stringify({ evidence, schema: backend.schema, runtime, source, repo, liveId, hybridId, baseline, final, live, hybrid, errors, screenshots, mutations: await backend.command('mutation_audit'), responses: await backend.command('response_audit'), reopenedWithoutPosts: true }, null, 2));
  } finally {
    try { const page = await desktop.firstWindow(); await writeFile(join(evidence, 'diagnostics.json'), JSON.stringify({ errors, text: await page.locator('body').innerText(), connection: await page.evaluate(() => window.agentgit.getConnection()), latestId, report: latestId ? await page.evaluate(experimentId => window.agentgit.getExperiment({ experimentId }), latestId) : null, mutations: await backend.command('mutation_audit') }, null, 2)); }
    finally { try { await desktop.close(); } finally { await backend.stop(); } }
  }
});
