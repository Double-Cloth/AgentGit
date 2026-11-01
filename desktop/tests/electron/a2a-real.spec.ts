import { test, expect, type Page } from '@playwright/test';
import { copyFile, mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { JsonObject } from '../../src/shared/bridge';
import { TOKEN, data, objects, readRun, startBackend } from '../support/real-client';
import { captureDesktop, desktopIdentity, launchDesktop } from '../support/desktop-runtime';

const qa = process.env.AGENTGIT_A2A_DESKTOP_QA ?? join(tmpdir(), 'agentgit-a2a-desktop-qa');
async function select(page: Page, repository: string) {
  await page.getByLabel('已有项目 ID').fill(repository);
  await page.getByRole('button', { name: '选择项目', exact: true }).click();
  await expect(page.getByRole('button', { name: 'API 已连接', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '收起', exact: true }).click();
}
async function prepare(page: Page, seed: JsonObject) {
  await page.getByRole('button', { name: 'A2A 远端任务', exact: true }).click();
  await page.getByRole('button', { name: '新建 A2A 任务', exact: true }).click();
  await page.getByRole('textbox', { name: '登记地址', exact: true }).fill(String(seed.endpoint));
  await page.getByRole('textbox', { name: '服务名称', exact: true }).fill(String(seed.server));
  await page.getByRole('textbox', { name: '非秘密主体', exact: true }).fill(String(seed.principal));
  await page.getByRole('button', { name: '发现并核对服务卡片', exact: true }).click();
  await expect(page.getByText('服务卡片已发现。', { exact: false })).toBeVisible();
}
async function openAccepted(page: Page) {
  await page.getByRole('button', { name: '打开新 A2A 时间线', exact: true }).click();
  await expect(page.locator('.adapter-summary')).toContainText('A2A 远端任务');
  return (await page.locator('.page-heading p > code').innerText()).trim();
}
async function status(page: Page, run: string, value: string) {
  await expect.poll(async () => (await readRun(page, run)).status, { timeout: 40000 }).toBe(value);
}
async function approve(page: Page) {
  await expect(page.getByRole('button', { name: '批准此动作', exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: '批准此动作', exact: true }).first().click();
}

test('A2A桌面真实Live审批、完整Recorded、状态产物与Diff及重开只读', async () => {
  await mkdir(qa, { recursive: true });
  const backend = await startBackend('a2a'), seed = await backend.command('seed');
  const profile = await mkdtemp(join(tmpdir(), 'agentgit-a2a-success-'));
  let desktop = await launchDesktop(profile, backend.url, TOKEN), page = await desktop.firstWindow();
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()); });
  try {
    const identity = await desktopIdentity(desktop, page); await select(page, String(seed.repository)); await prepare(page, seed);
    await page.setViewportSize({ width: 1440, height: 1100 });
    await expect(page.locator('dl[aria-label="A2A分支前置条件"]')).toContainText('无 HEAD');
    await expect(page.locator('dl[aria-label="A2A分支前置条件"] > div').nth(1).locator('dd')).toHaveText('0');
    const advanced = await backend.command('bump_branch');
    await page.getByRole('button', { name: '确认提交 A2A 任务', exact: true }).click();
    await expect(page.getByText('此次提交已明确拒绝，未受理。', { exact: false })).toBeVisible();
    await expect(page.getByRole('textbox', { name: '首条公开消息', exact: true })).toBeEnabled();
    await expect(page.getByRole('textbox', { name: '首条公开消息', exact: true })).toHaveValue('整理公开资料：Aster、Birch、Cedar。');
    await expect(page.getByText('服务卡片已发现。', { exact: false })).toBeVisible();
    const rejected = await backend.command('inspect'); expect((rejected.counts as JsonObject).jobs).toBe(0); expect(objects(rejected.calls)).toHaveLength(0);
    await page.getByText('此次提交已明确拒绝，未受理。', { exact: false }).scrollIntoViewIfNeeded();
    await captureDesktop(desktop, page, join(qa, 'conflict-1440.png'));
    await page.getByRole('button', { name: '刷新分支前置条件', exact: true }).click();
    await expect(page.locator('dl[aria-label="A2A分支前置条件"] > div').nth(1).locator('dd')).toHaveText(String(advanced.version));
    await page.locator('dl[aria-label="A2A分支前置条件"]').scrollIntoViewIfNeeded();
    await captureDesktop(desktop, page, join(qa, 'preconditions-1440.png'));
    await page.getByRole('heading', { name: '准备远端任务', exact: true }).scrollIntoViewIfNeeded();
    await captureDesktop(desktop, page, join(qa, 'prepare-1440.png'));
    await page.getByRole('button', { name: '确认提交 A2A 任务', exact: true }).click();
    const source = await openAccepted(page); await status(page, source, 'waiting_approval');
    let raw = await backend.command('inspect'); expect(objects(raw.calls)).toHaveLength(0);
    await approve(page); await status(page, source, 'completed');
    const sourceRun = await readRun(page, source), sourceAdapter = sourceRun.adapter as JsonObject;
    await expect(page.locator('.adapter-summary .facts > div').first()).toContainText('已完成');
    await expect(page.locator('.adapter-summary .facts > div').nth(1)).toContainText('已完成');
    for (const width of [1440, 768, 360]) {
      await page.setViewportSize({ width, height: 1100 }); await page.locator('.adapter-summary').scrollIntoViewIfNeeded();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await captureDesktop(desktop, page, join(qa, `live-${width}.png`));
    }
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.getByRole('tab', { name: '产物文件', exact: true }).click();
    await expect(page.locator('.files li')).not.toHaveCount(0);
    const artifacts = data(await page.evaluate(id => window.agentgit.getArtifacts({ runId: id }), source));
    const artifact = objects(artifacts.items)[0]!;
    expect(data(await page.evaluate(id => window.agentgit.readArtifact({ artifactId: id }), String(artifact.id))).totalBytes).toBeGreaterThan(0);
    raw = await backend.command('inspect'); const sourceFingerprints = raw.fingerprints as JsonObject, requests = objects((raw.protocol as JsonObject).requests).length;
    await copyFile(String(raw.evidence), join(qa, 'live-raw.json'));
    await page.getByRole('button', { name: 'A2A 成功轨迹回放 →', exact: true }).click();
    await page.getByRole('textbox', { name: 'Recorded 新分支名称', exact: true }).fill('a2a-desktop-recorded');
    await backend.command('recorded_guard');
    await page.getByRole('button', { name: '建立 Fork 并提交 A2A Recorded', exact: true }).click();
    await page.getByRole('button', { name: '打开 A2A Recorded 时间线', exact: true }).click();
    const target = (await page.locator('.page-heading p > code').innerText()).trim(); expect(target).not.toBe(source);
    await status(page, target, 'completed');
    const targetRun = await readRun(page, target), targetAdapter = targetRun.adapter as JsonObject;
    expect(targetAdapter.actual_attempts).toBe(0); expect(targetAdapter.actual_response_bytes).toBe(0);
    expect(targetAdapter.logical_state_ref).toBe(sourceAdapter.logical_state_ref); expect(targetAdapter.task_id).toBe(sourceAdapter.task_id);
    expect(targetAdapter.physical_state_ref).not.toBe(sourceAdapter.physical_state_ref);
    await expect(page.locator('.adapter-summary')).toContainText('历史来源');
    await expect(page.locator('.adapter-summary .facts > div').first()).toContainText('已完成');
    raw = await backend.command('inspect'); expect(objects((raw.protocol as JsonObject).requests)).toHaveLength(requests); expect(objects(raw.calls)).toHaveLength(1);
    expect((raw.fingerprints as JsonObject)[source]).toEqual(sourceFingerprints[source]);
    await copyFile(String(raw.evidence), join(qa, 'recorded-raw.json'));
    await page.locator('.adapter-summary').scrollIntoViewIfNeeded(); await captureDesktop(desktop, page, join(qa, 'recorded-1440.png'));
    await page.getByRole('button', { name: '差异', exact: true }).click();
    await page.getByLabel('BASE · 基线 Commit', { exact: true }).fill(String(sourceRun.checkpoint_commit_id));
    await page.getByLabel('HEAD · 对照 Commit', { exact: true }).fill(String(targetRun.checkpoint_commit_id));
    await page.getByRole('button', { name: '比较', exact: true }).click();
    await expect(page.locator('.diff-summary')).toContainText('项差异');
    await captureDesktop(desktop, page, join(qa, 'diff-1440.png'));
    const mutations = await backend.command('mutations'); await desktop.close();
    desktop = await launchDesktop(profile, backend.url, TOKEN); page = await desktop.firstWindow();
    page.on('pageerror', error => errors.push(error.message)); page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()); });
    await desktopIdentity(desktop, page);
    await page.getByPlaceholder('粘贴 UUID', { exact: true }).fill(target); await page.getByRole('button', { name: '打开', exact: true }).click();
    await expect(page.locator('.adapter-summary')).toContainText('本次仅重算历史逻辑状态与产物');
    expect(await backend.command('mutations')).toEqual(mutations); expect(errors).toEqual([]); expect(page.url()).toBe('agentgit://app/index.html'); await expect(page.locator('vite-error-overlay, nextjs-portal')).toHaveCount(0);
    await writeFile(join(qa, 'success-proof.json'), JSON.stringify({ identity, source, target, sourceAdapter, targetAdapter, requests, mutations, errors, rejected_counts: rejected.counts, refreshed_branch_version: advanced.version }, null, 2));
  } finally { await desktop.close(); await backend.stop(); }
});

test('A2A提交响应未确认、断连保留、原键只读确认与控制原请求', async () => {
  await mkdir(qa, { recursive: true });
  const backend = await startBackend('a2a'), seed = await backend.command('seed');
  const profile = await mkdtemp(join(tmpdir(), 'agentgit-a2a-retry-'));
  await backend.command('hold', { enabled: true });
  const desktop = await launchDesktop(profile, backend.url, TOKEN), page = await desktop.firstWindow();
  try {
    await desktopIdentity(desktop, page); await select(page, String(seed.repository)); await prepare(page, seed);
    await backend.command('lost_reply', { suffix: '/a2a-runs' });
    await page.getByRole('button', { name: '确认提交 A2A 任务', exact: true }).click();
    await expect(page.getByRole('button', { name: '只读确认原提交', exact: true })).toBeEnabled();
    await expect(page.getByRole('textbox', { name: '首条公开消息', exact: true })).toBeDisabled();
    await expect(page.locator('dl[aria-label="A2A分支前置条件"] > div').nth(1).locator('dd')).toHaveText('0');
    await backend.command('lost_reply', { suffix: '/a2a-runs' });
    await page.getByRole('button', { name: '显式重试原提交', exact: true }).click();
    await expect(page.getByRole('button', { name: '只读确认原提交', exact: true })).toBeEnabled();
    const before = await backend.command('mutations');
    const submits = objects(before.mutations).filter(item => String(item.path).endsWith('/a2a-runs'));
    expect(submits).toHaveLength(2); expect(submits[0]!.key).toBe(submits[1]!.key);
    await backend.command('offline', { enabled: true }); await expect(page.getByRole('button', { name: '连接设置', exact: true })).toBeVisible({ timeout: 20000 });
    await expect(page.getByRole('button', { name: '只读确认原提交', exact: true })).toBeDisabled();
    await page.setViewportSize({ width: 768, height: 1100 }); await page.getByRole('heading', { name: '准备远端任务', exact: true }).scrollIntoViewIfNeeded(); await captureDesktop(desktop, page, join(qa, 'offline-original-key-768.png'));
    await page.locator('.meter-attention').scrollIntoViewIfNeeded(); await captureDesktop(desktop, page, join(qa, 'pending-original-key-768.png'));
    await backend.command('offline', { enabled: false }); await expect(page.getByRole('button', { name: 'API 已连接', exact: true })).toBeVisible({ timeout: 20000 });
    await page.getByRole('button', { name: '只读确认原提交', exact: true }).click(); expect(await backend.command('mutations')).toEqual(before);
    const run = await openAccepted(page); await status(page, run, 'queued');
    await expect(page.locator('.adapter-summary .facts > div').first()).toContainText('排队中');
    await backend.command('bump_version', { run });
    await page.getByRole('button', { name: '请求暂停', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('version_conflict');
    await expect(page.getByRole('button', { name: '显式重试原控制', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '请求暂停', exact: true })).toBeEnabled();
    const version = (await backend.command('version', { run })).version;
    await backend.command('release_run_reads');
    await page.getByRole('button', { name: '刷新运行状态', exact: true }).click();
    await expect(page.locator('.run-summary')).toContainText(`状态版本 ${version}`);
    await backend.command('lost_reply', { suffix: '/pause' }); await page.getByRole('button', { name: '请求暂停', exact: true }).click();
    await expect(page.getByRole('button', { name: '显式重试原控制', exact: true })).toBeEnabled();
    await backend.command('hold', { enabled: false }); await status(page, run, 'paused');
    await page.getByRole('button', { name: '显式重试原控制', exact: true }).click();
    await expect(page.getByRole('button', { name: '恢复', exact: true })).toBeEnabled(); await page.getByRole('button', { name: '恢复', exact: true }).click();
    await status(page, run, 'waiting_approval');
    await expect(page.locator('.adapter-summary .facts > div').first()).toContainText('等待审批');
    await page.getByRole('button', { name: '请求取消', exact: true }).click(); await status(page, run, 'cancelled');
    const raw = await backend.command('inspect'); expect(objects(raw.calls)).toHaveLength(0); expect((raw.counts as JsonObject).jobs).toBe(1);
    const mutations = objects((await backend.command('mutations')).mutations).filter(row => String(row.path).endsWith('/pause'));
    expect(mutations).toHaveLength(3); expect(mutations[0]!.status).toBe(409); expect(mutations[1]!.key).toBe(mutations[2]!.key);
    await copyFile(String(raw.evidence), join(qa, 'retry-cancel-raw.json'));
    await writeFile(join(qa, 'retry-proof.json'), JSON.stringify({ run, before, mutations }, null, 2));
  } finally { await desktop.close(); await backend.stop(); }
});

for (const mode of ['working', 'disconnect_after_accept']) test(`A2A本地与远端分别确认：${mode}`, async () => {
  await mkdir(qa, { recursive: true });
  const backend = await startBackend('a2a'), seed = await backend.command('seed'); await backend.command('mode', { value: mode });
  const profile = await mkdtemp(join(tmpdir(), 'agentgit-a2a-control-'));
  const desktop = await launchDesktop(profile, backend.url, TOKEN), page = await desktop.firstWindow();
  try {
    await desktopIdentity(desktop, page); await select(page, String(seed.repository)); await prepare(page, seed); await page.getByRole('button', { name: '确认提交 A2A 任务', exact: true }).click();
    const run = await openAccepted(page); await status(page, run, 'waiting_approval');
    await expect(page.locator('.adapter-summary .facts > div').first()).toContainText('等待审批');
    await expect(page.getByRole('button', { name: '请求暂停', exact: true })).toBeDisabled(); await approve(page);
    if (mode === 'working') {
      await expect(page.locator('.adapter-summary')).toContainText('处理中');
      await page.getByRole('button', { name: '请求取消', exact: true }).click(); await status(page, run, 'waiting_approval');
      await expect(page.locator('.adapter-summary')).toContainText('等待独立审批'); await expect(page.locator('.approvals')).toContainText('cancel');
      await approve(page); await status(page, run, 'cancelled'); await expect(page.locator('.adapter-summary .facts > div').nth(2).locator('dd')).toHaveText('已确认');
      const value = (await readRun(page, run)).adapter as JsonObject; expect(value.remote_cancel_status).toBe('confirmed'); expect(value.remote_state).toBe('TASK_STATE_CANCELED');
    } else {
      await status(page, run, 'outcome_unknown'); const value = (await readRun(page, run)).adapter as JsonObject;
      expect(value.actual_attempts).toBeNull(); expect(value.registered_attempts).toBe(1);
      await expect(page.locator('.adapter-summary')).toContainText('尚无确认回执');
      const before = await backend.command('inspect'); await page.waitForTimeout(3500); const after = await backend.command('inspect');
      expect(objects((after.protocol as JsonObject).requests)).toHaveLength(objects((before.protocol as JsonObject).requests).length);
    }
    const raw = await backend.command('inspect'); await copyFile(String(raw.evidence), join(qa, `${mode}-raw.json`));
    await page.setViewportSize({ width: 1440, height: 1100 }); await page.locator('.adapter-summary').scrollIntoViewIfNeeded(); await captureDesktop(desktop, page, join(qa, `${mode}-1440.png`));
  } finally { await desktop.close(); await backend.stop(); }
});
