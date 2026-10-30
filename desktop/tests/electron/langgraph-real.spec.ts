import { test, expect, type Page } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TOKEN, data, objects, startBackend } from '../support/real-client';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

async function openRun(page: Page, runId: string) {
  await page.getByRole('button', { name: '运行', exact: true }).click();
  await page.getByPlaceholder('粘贴 UUID', { exact: true }).fill(runId);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'LangGraph 固定图', exact: true })).toBeVisible();
  await expect(page.locator('vite-error-overlay, nextjs-portal')).toHaveCount(0);
}
async function waitStatus(page: Page, runId: string, status: string) {
  await expect.poll(async () => {
    const reply = await page.evaluate(id => window.agentgit.getRun({ runId: id }), runId);
    return reply.ok ? reply.data.status : reply.error.code;
  }, { timeout: 30000 }).toBe(status);
}

for (const transport of ['http', 'stdio'] as const) {
  test(`真实 LangGraph ${transport}：原生暂停、来源与零调用分支回放、窄窗及重开`, async () => {
    const backend = await startBackend(transport === 'stdio' ? 'langgraph-stdio' : 'langgraph');
    const folder = await mkdtemp(join(tmpdir(), `agentgit-graph-${transport}-`));
    const seed = await backend.command('seed').catch(async failure => { await backend.stop(); throw failure; });
    const runId = String(seed.run_id), profile = join(folder, 'profile');
    let desktop = await launchDesktop(profile, backend.url, TOKEN).catch(async failure => { await backend.stop(); throw failure; });
    const errors: string[] = [], images: string[] = [];
    const observe = (page: Page) => { page.on('pageerror', error => errors.push(error.message)); page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()); }); };
    try {
      let page = await desktop.firstWindow(); observe(page); const runtime = await desktopIdentity(desktop, page);
      await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
      await openRun(page, runId); await waitStatus(page, runId, 'paused');
      const panel = page.locator('.adapter-summary');
      await expect(page.getByRole('button', { name: '查看故障报告', exact: true })).toHaveCount(0);
      await expect(page.getByText('预算与来源原文', { exact: true })).toHaveCount(0);
      await expect(panel).toContainText('无模型调用'); await expect(panel).toContainText('审阅'); await expect(panel).not.toContainText('Mock');
      await expect(panel.locator('div').filter({ has: page.locator('dt', { hasText: '本次工具尝试' }) }).first()).toContainText('1');
      await page.getByRole('button', { name: '分支方式说明 →', exact: true }).click();
      await expect(page.getByText('此任务使用固定程序，分支与回放目前由后端操作者提交。', { exact: false })).toBeVisible();
      await expect(page.getByRole('button', { name: '提交 Recorded', exact: true })).toBeDisabled();
      expect(objects((await backend.command('mutations')).mutations)).toHaveLength(0);
      await page.getByRole('button', { name: '查看来源时间线', exact: true }).click();
      for (const width of [1440, 360]) {
        await page.setViewportSize({ width, height: 1000 }); await page.locator('.adapter-summary').scrollIntoViewIfNeeded();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
        const path = join(folder, `graph-paused-${width}.png`); await captureDesktop(desktop, page, path); images.push(path);
      }
      await page.getByRole('button', { name: '恢复', exact: true }).click(); await waitStatus(page, runId, 'completed');
      await page.getByRole('tab', { name: '产物文件', exact: true }).click(); await expect(page.getByText('report.md', { exact: true }).first()).toBeVisible();
      const source = await backend.command('graph_inspect', { archive: true, label: 'completed' }); expect((source.counts as Record<string, unknown>).calls).toBe(1);
      const fork = await backend.command('graph_fork'); await waitStatus(page, String(fork.run_id), 'completed'); await openRun(page, String(fork.run_id));
      await expect(page.locator('.adapter-summary')).toContainText('备注恢复已完成');
      await expect(page.locator('.adapter-summary').getByText('0', { exact: true })).toBeVisible();
      const replay = await backend.command('graph_recorded'); await waitStatus(page, String(replay.run_id), 'completed'); await openRun(page, String(replay.run_id));
      await expect(page.locator('.adapter-summary')).toContainText('Recorded 消费固定来源范围');
      await expect(page.locator('.adapter-summary').getByText('0', { exact: true })).toBeVisible();
      await expect(page.getByText('逻辑状态、历史用量、实际零调用与逐检查点证明见下方 Replay 报告。', { exact: false })).toHaveCount(0);
      const recorded = await backend.command('graph_inspect', { run: String(replay.run_id), archive: true, label: 'recorded' });
      expect(recorded.counts).toEqual(source.counts); expect(recorded.source_fingerprint).toBe(source.source_fingerprint); expect(recorded.source_branch).toEqual(source.source_branch);
      const path = join(folder, 'graph-recorded-360.png'); await page.locator('.adapter-summary').scrollIntoViewIfNeeded(); await captureDesktop(desktop, page, path); images.push(path);
      await page.getByRole('button', { name: '打开来源运行', exact: true }).click(); await waitStatus(page, runId, 'completed');
      const mutations = objects((await backend.command('mutations')).mutations); expect(mutations).toHaveLength(1); expect(String(mutations[0]?.path)).toContain('/resume');
      await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); observe(page); await desktopIdentity(desktop, page);
      await openRun(page, String(replay.run_id)); await waitStatus(page, String(replay.run_id), 'completed'); await expect(page.locator('.adapter-summary')).toContainText('Recorded 消费固定来源范围');
      expect(objects((await backend.command('mutations')).mutations)).toHaveLength(mutations.length);
      const reopened = await backend.command('graph_inspect', { run: String(replay.run_id), archive: true, label: 'reopened' }); expect(reopened.counts).toEqual(source.counts);
      expect(errors).toEqual([]); await writeFile(join(folder, 'graph.json'), JSON.stringify({ runtime, seed, source, fork, replay, recorded, reopened, images, errors, mutations }, null, 2));
      console.log(`真实 LangGraph 桌面证据：${folder}`);
    } catch (failure) {
      const page = await desktop.firstWindow(); await writeFile(join(folder, 'failure.json'), JSON.stringify({ seed, errors, inspected: await backend.command('graph_inspect', { archive: true, label: 'failure' }), mutations: await backend.command('mutations'), text: await page.locator('body').innerText() }, null, 2));
      await page.screenshot({ path: join(folder, 'failure.png'), fullPage: true }); console.log(`真实 LangGraph 桌面失败证据：${folder}`); throw failure;
    } finally { await desktop.close(); await backend.stop(); }
  });

  for (const action of ['pause', 'cancel'] as const) test(`真实 LangGraph ${transport}：原生取证边界请求 ${action}`, async () => {
    const backend = await startBackend(transport === 'stdio' ? 'langgraph-stdio-boundary' : 'langgraph-boundary');
    const folder = await mkdtemp(join(tmpdir(), `agentgit-graph-${transport}-${action}-`));
    const seed = await backend.command('seed').catch(async failure => { await backend.stop(); throw failure; });
    const runId = String(seed.run_id);
    const desktop = await launchDesktop(join(folder, 'profile'), backend.url, TOKEN).catch(async failure => { await backend.stop(); throw failure; });
    const errors: string[] = [];
    try {
      const page = await desktop.firstWindow(); page.on('pageerror', error => errors.push(error.message)); page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()); });
      const runtime = await desktopIdentity(desktop, page); await backend.command('graph_gate', { enabled: true });
      await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click(); await openRun(page, runId);
      await page.getByRole('button', { name: action === 'pause' ? '请求暂停' : '请求取消', exact: true }).click();
      await expect.poll(async () => data(await page.evaluate(runId => window.agentgit.getRun({ runId }), runId)).control_status).toBe('requested');
      const requested = await backend.command('graph_inspect', { archive: true, label: 'requested' }); expect((requested.counts as Record<string, unknown>).calls).toBe(1);
      await backend.command('graph_gate', { enabled: false }); await waitStatus(page, runId, action === 'pause' ? 'paused' : 'cancelled');
      if (action === 'pause') { await page.getByRole('button', { name: '恢复', exact: true }).click(); await waitStatus(page, runId, 'completed'); }
      else { await expect(page.getByRole('button', { name: '恢复', exact: true })).toBeDisabled(); }
      const final = await backend.command('graph_inspect', { archive: true, label: 'final' }); expect(final.counts).toEqual(requested.counts); expect(errors).toEqual([]);
      const mutations = objects((await backend.command('mutations')).mutations); expect(mutations).toHaveLength(action === 'pause' ? 2 : 1);
      const path = join(folder, 'graph-control.png'); await page.locator('.adapter-summary').scrollIntoViewIfNeeded(); await captureDesktop(desktop, page, path);
      await writeFile(join(folder, 'graph.json'), JSON.stringify({ runtime, seed, requested, final, errors, mutations, images: [path] }, null, 2)); console.log(`真实 LangGraph 控制证据：${folder}`);
    } finally { await desktop.close(); await backend.stop(); }
  });
}
