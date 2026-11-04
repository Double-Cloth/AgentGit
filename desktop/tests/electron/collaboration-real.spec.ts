import { test, expect, type Page } from '@playwright/test';
import { copyFile, mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TOKEN, data, objects, startBackend } from '../support/real-client';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const qa = process.env.AGENTGIT_COLLABORATION_DESKTOP_QA ?? join(tmpdir(), 'agentgit-collaboration-desktop');
async function select(page: Page, repo: string) {
  await page.getByLabel('已有项目 ID').fill(repo); await page.getByRole('button', { name: '选择项目', exact: true }).click();
  await page.getByRole('button', { name: '收起', exact: true }).click(); await page.getByRole('button', { name: '四角色协作', exact: true }).click();
}
async function current(page: Page) { return (await page.locator('.collaboration-detail > p > code').innerText()).trim(); }
async function status(page: Page, id: string, expected: string) {
  await expect.poll(async () => data(await page.evaluate(groupId => window.agentgit.getCollaboration({ groupId }), id)).status, { timeout: 90000 }).toBe(expected);
}
async function prepare(page: Page, privateNote = '') {
  await page.getByRole('button', { name: '准备新协作', exact: true }).click();
  if (privateNote) { await page.getByText('角色预算、权限与私有备注', { exact: true }).click(); await page.getByLabel('Researcher · 取证 私有备注', { exact: true }).fill(privateNote); }
  await page.getByRole('button', { name: '确认准备协作', exact: true }).click();
}

test('S12-C 真实桌面准备、组暂停恢复、四角色来源及 Recorded 零执行重开', async () => {
  await mkdir(qa, { recursive: true });
  const backend = await startBackend('collaboration'), seed = await backend.command('seed');
  const profile = await mkdtemp(join(tmpdir(), 'agentgit-collaboration-profile-'));
  let desktop = await launchDesktop(profile, backend.url, TOKEN), page = await desktop.firstWindow();
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  try {
    const identity = await desktopIdentity(desktop, page); await select(page, String(seed.repository));
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.getByRole('button', { name: '准备新协作', exact: true }).click();
    await page.getByText('角色预算、权限与私有备注', { exact: true }).click();
    await page.getByRole('textbox', { name: 'Researcher · 取证 私有备注', exact: true }).fill('private-desktop-research-only');
    await page.getByRole('heading', { name: '准备四角色研究', exact: true }).scrollIntoViewIfNeeded();
    await captureDesktop(desktop, page, join(qa, 'prepare-1440.png'));
    await page.getByRole('button', { name: '确认准备协作', exact: true }).click();
    const source = await current(page); await status(page, source, 'prepared');
    let snapshot = await backend.command('inspect'); expect(objects(snapshot.calls)).toHaveLength(0); expect(objects(snapshot.groups)).toHaveLength(1);
    await page.getByRole('button', { name: '启动协作', exact: true }).click(); await status(page, source, 'running');
    await page.getByRole('button', { name: '请求整组暂停', exact: true }).click(); await status(page, source, 'paused');
    await expect(page.getByRole('button', { name: '恢复整组', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '恢复整组', exact: true }).click(); await status(page, source, 'running');
    await backend.command('hold', { enabled: false }); await status(page, source, 'completed');
    await expect(page.getByRole('table', { name: '协作角色', exact: true }).locator('tbody tr')).toHaveCount(4);
    await expect(page.locator('.collaboration-evidence').first().locator(':scope > details')).toHaveCount(7);
    await expect(page.getByRole('table', { name: '共享产物版本', exact: true }).locator('tbody tr')).toHaveCount(5);
    expect(await page.locator('.collaboration-detail').innerText()).not.toContain('private-desktop-research-only');
    snapshot = await backend.command('inspect'); expect(objects(snapshot.calls)).toHaveLength(20);
    const sourceFingerprints = snapshot.fingerprints;
    await copyFile(String(snapshot.evidence), join(qa, 'source-raw.json'));
    await page.evaluate(() => window.scrollTo(0, 0));
    await captureDesktop(desktop, page, join(qa, 'workspace-1440.png'));
    for (const width of [1440, 768, 360]) {
      await page.setViewportSize({ width, height: 1100 });
      await page.locator('.collaboration-detail').scrollIntoViewIfNeeded();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await captureDesktop(desktop, page, join(qa, `group-${width}.png`));
    }
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.locator('.collaboration-evidence').first().scrollIntoViewIfNeeded();
    await page.locator('.collaboration-evidence').first().locator(':scope > details').first().locator('summary').click();
    await captureDesktop(desktop, page, join(qa, 'messages-1440.png'));
    await page.getByRole('table', { name: '共享产物版本', exact: true }).scrollIntoViewIfNeeded();
    await captureDesktop(desktop, page, join(qa, 'shared-1440.png'));
    await page.getByRole('table', { name: '协作角色', exact: true }).getByRole('button', { name: '查看角色时间线', exact: true }).first().click();
    await expect(page.locator('.adapter-summary')).toContainText('四角色协作');
    await expect(page.locator('.adapter-summary')).not.toContainText('工具来源待核对');
    await page.getByRole('button', { name: '打开所属协作', exact: true }).click();
    await backend.command('recorded_guard');
    await page.getByRole('button', { name: '准备 Recorded 协作', exact: true }).click();
    await expect.poll(() => current(page)).not.toBe(source);
    const target = await current(page); await status(page, target, 'prepared');
    await page.getByRole('button', { name: '启动协作', exact: true }).click(); await status(page, target, 'completed');
    snapshot = await backend.command('inspect');
    const calls = objects(snapshot.calls), reused = calls.filter(item => Boolean(item.origin_call_id));
    expect(calls).toHaveLength(40); expect(reused).toHaveLength(20);
    for (const [id, value] of Object.entries(sourceFingerprints as object)) expect((snapshot.fingerprints as Record<string, unknown>)[id]).toEqual(value);
    await copyFile(String(snapshot.evidence), join(qa, 'recorded-raw.json'));
    const mutations = await backend.command('mutations');
    await desktop.close(); desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow();
    await desktopIdentity(desktop, page); await page.getByRole('button', { name: '四角色协作', exact: true }).click();
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.getByPlaceholder('粘贴协作 UUID', { exact: true }).fill(target); await page.getByRole('button', { name: '按 ID 打开协作', exact: true }).click();
    await expect(page.locator('.collaboration-detail')).toContainText('所有角色已静止');
    expect(await backend.command('mutations')).toEqual(mutations);
    expect(page.url()).toBe('agentgit://app/index.html'); expect(errors).toEqual([]); expect(await page.locator('vite-error-overlay').count()).toBe(0);
    await captureDesktop(desktop, page, join(qa, 'recorded-reopened-1440.png'));
    await writeFile(join(qa, 'success-proof.json'), JSON.stringify({ identity, source, target, mutations, errors, sourceFingerprints, counts: snapshot.counts }, null, 2));
  } finally { await desktop.close(); await backend.stop(); }
});

test('S12-C 响应丢失后原键显式重试，断连保留状态且重开只读', async () => {
  await mkdir(qa, { recursive: true });
  const backend = await startBackend('collaboration'), seed = await backend.command('seed');
  const profile = await mkdtemp(join(tmpdir(), 'agentgit-collaboration-retry-'));
  let desktop = await launchDesktop(profile, backend.url, TOKEN), page = await desktop.firstWindow();
  try {
    await desktopIdentity(desktop, page); await select(page, String(seed.repository));
    await backend.command('lost_reply', { suffix: '/prepare' }); await prepare(page);
    await expect(page.getByRole('button', { name: '重试原准备请求', exact: true })).toBeVisible();
    await expect(page.getByRole('textbox', { name: '协作目标', exact: true })).toBeDisabled();
    let mutations = objects((await backend.command('mutations')).mutations); expect(mutations).toHaveLength(1);
    await page.getByRole('button', { name: '重试原准备请求', exact: true }).click();
    const id = await current(page); await status(page, id, 'prepared');
    mutations = objects((await backend.command('mutations')).mutations); expect(mutations).toHaveLength(2); expect(mutations[0]!.key).toBe(mutations[1]!.key);
    expect(objects((await backend.command('inspect')).groups)).toHaveLength(1);
    await backend.command('lost_reply', { suffix: '/start' }); await page.getByRole('button', { name: '启动协作', exact: true }).click();
    await expect(page.getByRole('button', { name: '重试原组请求', exact: true })).toBeVisible();
    await backend.command('offline', { enabled: true });
    await expect(page.getByRole('button', { name: '连接设置', exact: true })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: '重试原组请求', exact: true })).toBeDisabled();
    await expect(page.locator('.collaboration-detail')).toContainText(id);
    await page.setViewportSize({ width: 768, height: 1100 });
    await captureDesktop(desktop, page, join(qa, 'offline-retry-768.png'));
    const before = await backend.command('mutations');
    await backend.command('offline', { enabled: false });
    await expect(page.getByRole('button', { name: 'API 已连接', exact: true })).toBeVisible({ timeout: 15000 });
    expect(await backend.command('mutations')).toEqual(before);
    await page.getByRole('button', { name: '重试原组请求', exact: true }).click();
    await expect(page.getByRole('button', { name: '请求整组暂停', exact: true })).toBeEnabled();
    await backend.command('lost_reply', { suffix: '/control' }); await page.getByRole('button', { name: '请求整组暂停', exact: true }).click();
    await expect(page.getByRole('button', { name: '重试原组请求', exact: true })).toBeVisible(); await status(page, id, 'paused');
    await page.getByRole('button', { name: '重试原组请求', exact: true }).click();
    await expect(page.getByRole('button', { name: '恢复整组', exact: true })).toBeEnabled();
    mutations = objects((await backend.command('mutations')).mutations);
    expect(mutations).toHaveLength(6); expect(mutations[2]!.key).toBe(mutations[3]!.key); expect(mutations[4]!.key).toBe(mutations[5]!.key);
    await page.getByRole('button', { name: '请求整组取消', exact: true }).click(); await status(page, id, 'cancelled');
    const snapshot = await backend.command('inspect'); expect(objects(snapshot.calls)).toHaveLength(0);
    await copyFile(String(snapshot.evidence), join(qa, 'retry-cancel-raw.json'));
    const finalMutations = await backend.command('mutations'); await desktop.close();
    desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow(); await desktopIdentity(desktop, page);
    await page.getByRole('button', { name: '四角色协作', exact: true }).click(); await expect(page.getByRole('table', { name: '协作组列表', exact: true })).toContainText('已取消');
    expect(await backend.command('mutations')).toEqual(finalMutations);
    await writeFile(join(qa, 'retry-proof.json'), JSON.stringify({ id, mutations: finalMutations, counts: snapshot.counts }, null, 2));
  } finally { await desktop.close(); await backend.stop(); }
});
