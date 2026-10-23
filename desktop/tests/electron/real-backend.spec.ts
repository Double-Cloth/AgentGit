import { test, expect, _electron as electron, type Page } from '@playwright/test';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import type { BinaryChunk, JsonObject, Reply } from '../../src/shared/bridge';

const TOKEN = 'fixture-real-desktop-token-000000000000000000000';
function data<T>(reply: Reply<T>): T { if (!reply.ok) throw new Error(`${reply.error.code}: ${reply.error.message}`); return reply.data; }
function objects(value: unknown): JsonObject[] { return Array.isArray(value) ? value as JsonObject[] : []; }
async function startBackend() {
  const child = spawn('uv', ['run', '--locked', 'python', 'desktop/tests/support/real_backend.py'], {
    cwd: resolve('..'), windowsHide: true, env: { ...process.env, AGENTGIT_API_TOKEN: TOKEN, PYTHONIOENCODING: 'utf-8' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let logs = '', sequence = 0;
  const diagnostics = () => logs.replace(/^.*"event": "http.request".*\r?\n/gm, '');
  child.stderr.on('data', bytes => { logs = (logs + String(bytes)).slice(-64000); });
  const pending = new Map<number, { resolve: (value: JsonObject) => void; reject: (error: Error) => void }>();
  let readyResolve: (value: { url: string; schema: string }) => void, readyReject: (error: Error) => void;
  const ready = new Promise<{ url: string; schema: string }>((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const finished = new Promise<number | null>(resolve => child.once('exit', resolve));
  createInterface({ input: child.stdout }).on('line', line => {
    try { const message = JSON.parse(line); if (message.ready) readyResolve(message);
      else if (pending.has(message.id)) { pending.get(message.id)!.resolve(message.result); pending.delete(message.id); }
    } catch { readyReject(new Error('联调进程输出无效')); }
  });
  child.once('error', error => readyReject(error));
  child.once('exit', code => { const error = new Error(`联调进程结束 ${code}\n${diagnostics()}`); readyReject(error); for (const value of pending.values()) value.reject(error); });
  const address = await ready;
  return { ...address,
    command: (command: string, args: JsonObject = {}) => new Promise<JsonObject>((resolve, reject) => {
      const id = ++sequence; pending.set(id, { resolve, reject }); child.stdin.write(JSON.stringify({ id, command, ...args }) + '\n');
    }),
    stop: async () => { child.stdin.end(JSON.stringify({ command: 'stop' }) + '\n'); expect(await finished, diagnostics()).toBe(0); },
  };
}
async function readRun(page: Page, runId: string) { return data(await page.evaluate(id => window.agentgit.getRun({ runId: id }), runId)); }
async function completed(page: Page, runId: string) {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    const state = await readRun(page, runId);
    if (state.status === 'completed') return state;
    if (['failed', 'cancelled', 'budget_exhausted', 'outcome_unknown'].includes(String(state.status))) throw new Error(`Run 非成功终态：${JSON.stringify(state)}`);
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error('真实后端运行未在期限内完成');
}
async function sourceBranch(page: Page, sourceRun: string) {
  await page.getByRole('button', { name: '运行', exact: true }).click();
  await page.getByLabel('标识', { exact: true }).fill(sourceRun);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '从所选检查点分支 →' }).click();
  await expect(page.getByRole('heading', { name: '分支实验', exact: true })).toBeVisible();
}

test('真实设施：桌面五工具 → 历史/Fork → 零执行 Recorded → A/B Live → Diff/导出', async () => {
  const backend = await startBackend();
  const profile = await mkdtemp(join(tmpdir(), 'agentgit-real-profile-'));
  const executable = process.env.AGENTGIT_DESKTOP_EXECUTABLE;
  const env: Record<string, string> = { ...Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined)),
    AGENTGIT_API_TOKEN: TOKEN, AGENTGIT_API_URL: backend.url, AGENTGIT_DESKTOP_PROFILE_DIR: profile };
  if (executable) {
    // 打包程序从独立目录启动；子进程 PATH 不包含构建用 Node/uv/npm。
    for (const key of Object.keys(env)) if (key.toLowerCase() === 'path') delete env[key];
    env.PATH = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32');
  }
  const desktop = await electron.launch({ ...(executable ? { executablePath: resolve(executable), args: [], cwd: profile }
    : { args: ['.'], cwd: resolve('.') }), env }).catch(async error => { await backend.stop(); throw error; });
  await desktop.context().tracing.start({ screenshots: true, snapshots: true });
  let currentPage: Page | undefined;
  try {
    const page = await desktop.firstWindow();
    currentPage = page;
    if (executable) {
      expect(await desktop.evaluate(({ app }) => app.isPackaged)).toBe(true);
      expect(await desktop.evaluate(() => process.env.PATH)).toBe(env.PATH);
    }
    await expect(page.getByText('API 与数据库可用；Worker')).toBeVisible();
    await page.getByLabel('新项目名称').fill('S6 真实桌面五工具与隔离分支');
    await page.getByRole('button', { name: '创建并选择', exact: true }).click();
    await expect(page.getByRole('heading', { name: '运行', exact: true })).toBeVisible();
    const repo = (await page.locator('.contextbar code').innerText()).trim();
    await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '新建运行', exact: true }).click();
    await expect(page.getByLabel('启用固定五工具')).toBeChecked();
    await page.getByRole('button', { name: '提交运行', exact: true }).click();
    await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    const source = objects(data(await page.evaluate(() => window.agentgit.listRuns({}))).items)[0]!;
    const sourceRun = String(source.id), sourceState = await completed(page, sourceRun);
    expect((sourceState.budget as JsonObject).usage).toMatchObject({ tool_calls: 13, model_calls: 15 });
    const baseline = await backend.command('audit', { repository: repo, source_run: sourceRun });
    expect(baseline.statistics).toMatchObject({ claims: 9, frameworks: 3, sources: 6 });
    const history = objects(data(await page.evaluate(() => window.agentgit.listCommits({ branchName: 'main', limit: 100 }))).items);
    const start = String(history.at(-1)!.commit_id), end = String(sourceState.checkpoint_commit_id);
    let terminal = '';
    for (const item of history) {
      const state = data(await page.evaluate(commitId => window.agentgit.getCommit({ commitId }), String(item.commit_id)));
      const pending = ((state.snapshot as JsonObject).data as JsonObject).pending_tool as JsonObject | null;
      if (pending?.tool === 'terminal_exec') { terminal = String(item.commit_id); break; }
    }
    expect(terminal).toHaveLength(64);
    await page.getByRole('tab', { name: '执行证据', exact: true }).click();
    await expect(page.getByLabel('执行调用')).toBeVisible();
    await expect(page.getByText('statistics written', { exact: false })).toBeVisible();
    await page.getByLabel('原始证据').selectOption('code');
    await expect(page.getByText('claims_per_framework', { exact: false })).toBeVisible();
    await page.getByRole('tab', { name: '产物文件', exact: true }).click();
    await expect(page.getByRole('button', { name: 'results/statistics.json', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'results/statistics.json', exact: true }).click();
    await expect(page.getByText(/"claims":\s*9/)).toBeVisible();
    await page.screenshot({ path: '../.agentgit/evidence/S6-real-source.png', fullPage: true });
    await sourceBranch(page, sourceRun);
    await page.getByRole('radio', { name: /Fork/ }).check();
    await page.getByLabel('安全起点 Commit').fill(terminal); await page.getByLabel('新分支名称').fill('desktop-fork');
    await page.getByRole('button', { name: '创建分支', exact: true }).click();
    await expect(page.getByRole('heading', { name: '已受理', exact: true })).toBeVisible();
    await sourceBranch(page, sourceRun);
    await backend.command('guard', { enabled: true });
    await page.getByLabel('安全起点 Commit').fill(start); await page.getByLabel('验证终点 Commit').fill(end);
    await page.getByLabel('新分支名称').fill('desktop-recorded');
    await page.getByRole('button', { name: '提交 Recorded', exact: true }).click();
    await expect(page.getByRole('heading', { name: '已受理', exact: true })).toBeVisible();
    let runs = objects(data(await page.evaluate(() => window.agentgit.listRuns({}))).items);
    const recorded = runs.find(item => item.branch_name === 'desktop-recorded')!;
    const verified = await completed(page, String(recorded.id));
    expect(verified.replay).toMatchObject({ verified: true, logical_status: 'completed', actual: { model_calls: 0, tool_calls: 0 } });
    await backend.command('guard', { enabled: false });
    const afterRecorded = await backend.command('audit', { repository: repo, source_run: sourceRun });
    expect(afterRecorded.recorded_engine_guard_hits).toBe(0); expect(afterRecorded.recorded_model_guard_hits).toBe(0);
    expect(objects(afterRecorded.executions)).toHaveLength(2);
    expect(afterRecorded.source_fingerprint).toBe(baseline.source_fingerprint);
    const lives: { runId: string; head: string; artifactId: string; objectId: string; text: string }[] = [];
    for (const label of ['A', 'B']) {
      await sourceBranch(page, sourceRun); await page.getByRole('radio', { name: /Live/ }).check();
      await page.getByLabel('安全起点 Commit').fill(terminal); await page.getByLabel('新分支名称').fill(`desktop-live-${label}`);
      await page.getByLabel('干预 overrides JSON').fill(JSON.stringify({ schema_version: 1,
        artifacts: [{ path: 'execution/organize.sh', text: `set -eu\nmkdir -p results\nprintf ${label} > results/desktop-same.txt\n` }],
        execution: { tool: 'terminal_exec', path: 'execution/organize.sh' } }));
      await page.getByRole('button', { name: '提交 Live', exact: true }).click();
      await expect(page.getByRole('heading', { name: '已受理', exact: true })).toBeVisible();
      runs = objects(data(await page.evaluate(() => window.agentgit.listRuns({}))).items);
      const live = runs.find(item => item.branch_name === `desktop-live-${label}`)!;
      const state = await completed(page, String(live.id));
      expect(state.replay).toMatchObject({ mode: 'live', actual: { model_calls: 1, tool_calls: 1 } });
      const files = objects(data(await page.evaluate(commitId => window.agentgit.getCommitArtifacts({ commitId }), String(state.checkpoint_commit_id))).items);
      const file = files.find(item => item.relative_path === 'results/desktop-same.txt')!;
      const chunk = data(await page.evaluate(artifactId => window.agentgit.readArtifact({ artifactId }), String(file.id))) as BinaryChunk;
      expect(Buffer.from(chunk.base64, 'base64').toString()).toBe(label);
      lives.push({ runId: String(live.id), head: String(state.checkpoint_commit_id), artifactId: String(file.id), objectId: String(file.object_id), text: label });
    }
    expect(lives[0]!.objectId).not.toBe(lives[1]!.objectId);
    await page.getByRole('button', { name: '差异', exact: true }).click();
    await page.getByLabel('BASE · 基线 Commit', { exact: true }).fill(lives[0]!.head);
    await page.getByLabel('HEAD · 对照 Commit', { exact: true }).fill(lives[1]!.head);
    await page.getByRole('button', { name: '比较', exact: true }).click();
    await expect(page.getByRole('button', { name: /artifacts\/results~1desktop-same.txt/ })).toBeVisible();
    await page.getByRole('button', { name: /artifacts\/results~1desktop-same.txt/ }).click();
    await expect(page.getByText(`"${lives[0]!.objectId}"`, { exact: true })).toBeVisible();
    await expect(page.getByText(`"${lives[1]!.objectId}"`, { exact: true })).toBeVisible();
    await page.screenshot({ path: '../.agentgit/evidence/S6-real-diff.png', fullPage: true });
    const exportPath = join(profile, 'export-A.bin');
    await desktop.evaluate(({ dialog }, filePath) => { dialog.showSaveDialog = async () => ({ canceled: false, filePath }); }, exportPath);
    const exported = data(await page.evaluate(artifactId => window.agentgit.exportArtifact({ artifactId }), lives[0]!.artifactId));
    expect(exported).toMatchObject({ saved: true, cancelled: false }); expect(await readFile(exportPath, 'utf8')).toBe('A');
    const final = await backend.command('audit', { repository: repo, source_run: sourceRun });
    expect(final.source_fingerprint).toBe(baseline.source_fingerprint); expect(final.source_branch).toEqual(baseline.source_branch);
    const executions = objects(final.executions);
    expect(executions).toHaveLength(4); expect(new Set(executions.map(item => item.container_name)).size).toBe(4);
    expect(executions.every(item => item.cleanup_confirmed)).toBe(true);
    await writeFile('../.agentgit/evidence/S6-real-desktop-audit.json', JSON.stringify({ repository: repo, sourceRun,
      sourceHead: end, recorded: verified.replay, lives, final, package: executable ?? null,
      export: { bytes: 'A', dialog: '测试替换原生对话框返回值，真实主进程写入' } }, null, 2));
  } catch (error) {
    if (currentPage && !currentPage.isClosed()) {
      await currentPage.screenshot({ path: '../.agentgit/evidence/S6-real-failure.png', fullPage: true });
      await writeFile('../.agentgit/evidence/S6-real-failure.txt', await currentPage.locator('body').innerText());
    }
    throw error;
  } finally {
    await desktop.context().tracing.stop({ path: '../.agentgit/evidence/S6-real-electron-trace.zip' });
    await desktop.close(); await backend.stop();
  }
});
