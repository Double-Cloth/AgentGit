import { test, expect } from '@playwright/test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TOKEN, data, objects, startBackend } from '../support/real-client';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

for (const transport of ['http', 'stdio'] as const) test(`真实 MCP ${transport} 任务：原生审批、零模型调用、逻辑状态及产物、重开只读`, async () => {
  const backend = await startBackend(transport === 'stdio' ? 'mcp-stdio' : 'mcp'), folder = await mkdtemp(join(tmpdir(), `agentgit-s12-mcp-${transport}-`));
  const seed = await backend.command('seed').catch(async failure => { await backend.stop(); throw failure; });
  const runId = String(seed.run_id), profile = join(folder, 'profile');
  let desktop = await launchDesktop(profile, backend.url, TOKEN).catch(async failure => { await backend.stop(); throw failure; });
  const errors: string[] = [], images: string[] = [];
  const observe = (page: Awaited<ReturnType<typeof desktop.firstWindow>>) => { page.on('pageerror', error => errors.push(error.message)); page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); }); };
  try {
    let page = await desktop.firstWindow(); observe(page); const runtime = await desktopIdentity(desktop, page);
    await page.getByLabel('已有项目 ID').fill(String(seed.repository)); await page.getByRole('button', { name: '选择项目', exact: true }).click(); await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByPlaceholder('粘贴 UUID', { exact: true }).fill(runId); await page.getByRole('button', { name: '打开', exact: true }).click();
    await expect(page.getByRole('button', { name: '批准此动作', exact: true })).toBeEnabled();
    const original = await backend.command('mcp_inspect'); expect((original.counts as Record<string, unknown>).calls).toBe(0); expect(original.model_calls).toBe(0);
    await page.getByRole('button', { name: '批准此动作', exact: true }).click();
    await expect.poll(async () => data(await page.evaluate(runId => window.agentgit.getRun({ runId }), runId)).status, { timeout: 30000 }).toBe('completed');
    await page.getByRole('tab', { name: '逻辑状态', exact: true }).click(); await expect(page.getByRole('heading', { name: '所选版本状态', exact: true })).toBeVisible();
    await expect(page.getByText(/mcp_run\/v1/).first()).toBeVisible();
    await expect(page.locator('.inspector pre').first()).toContainText('"status": "completed"');
    for (const width of [1440, 360]) { await page.setViewportSize({ width, height: 1000 }); await page.getByRole('heading', { name: '所选版本状态', exact: true }).scrollIntoViewIfNeeded(); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true); const path = join(folder, `mcp-${width}.png`); await captureDesktop(desktop, page, path); images.push(path); }
    await page.getByRole('tab', { name: '产物文件', exact: true }).click(); await expect(page.getByText('mcp-result.json', { exact: true }).first()).toBeVisible();
    const completed = await backend.command('mcp_inspect', { archive: true, label: 'completed' }); expect((completed.counts as Record<string, unknown>).calls).toBe(1); expect(completed.model_calls).toBe(0);
    const mutations = objects((await backend.command('mutations')).mutations); expect(mutations).toHaveLength(1); expect(String(mutations[0]?.path)).toContain('/decision');
    await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); observe(page); await desktopIdentity(desktop, page);
    await page.getByPlaceholder('粘贴 UUID', { exact: true }).fill(runId); await page.getByRole('button', { name: '打开', exact: true }).click();
    await expect.poll(async () => { const reply = await page.evaluate(runId => window.agentgit.getRun({ runId }), runId); if (!reply.ok && reply.error.code === 'request_cancelled') return 'read_cancelled'; return data(reply).status; }).toBe('completed');
    expect(objects((await backend.command('mutations')).mutations)).toHaveLength(mutations.length);
    const reopened = await backend.command('mcp_inspect', { archive: true, label: 'reopened' }); expect(reopened.counts).toEqual(completed.counts); expect(errors).toEqual([]);
    await writeFile(join(folder, 'mcp.json'), JSON.stringify({ runtime, seed, original, completed, reopened, images, errors, mutations }, null, 2));
    console.log(`真实 MCP 桌面证据：${folder}`);
  } catch (failure) {
    const page = await desktop.firstWindow();
    await writeFile(join(folder, 'failure.json'), JSON.stringify({ seed, errors, inspected: await backend.command('mcp_inspect', { archive: true, label: 'failure' }), mutations: await backend.command('mutations'), text: await page.locator('body').innerText() }, null, 2));
    await page.screenshot({ path: join(folder, 'failure.png'), fullPage: true });
    console.log(`真实 MCP 桌面失败证据：${folder}`);
    throw failure;
  } finally { await desktop.close(); await backend.stop(); }
});
