import { test, expect, _electron as electron } from '@playwright/test';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { copyFile, mkdtemp, readFile, realpath, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, isAbsolute, join, relative, resolve } from 'node:path';
import { TOKEN, completed, data, objects, startBackend } from '../support/real-client';

const digest = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex');
async function exists(path: string) {
  try { await stat(path); return true; } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false; throw error; }
}
function inside(root: string, path: string) { const rel = relative(root, path); return !isAbsolute(rel) && rel !== '..' && !rel.startsWith('..\\'); }

test('当前用户安装：0.1.0 → 0.1.1 覆盖升级 → 卸载保留业务数据 → 重装查询与新任务', async () => {
  expect(process.platform).toBe('win32');
  expect(process.env.AGENTGIT_INSTALL_TEST, '仅在本次任务拥有的安装环境显式开启').toBe('1');
  const root = resolve(process.env.LOCALAPPDATA!, 'AgentGit');
  const physicalRoot = resolve(process.env.AGENTGIT_INSTALL_PHYSICAL_ROOT ?? root);
  expect(basename(physicalRoot)).toBe('AgentGit');
  expect(inside(resolve(process.env.LOCALAPPDATA!), physicalRoot)).toBe(true);
  const profile = resolve(process.env.APPDATA!, 'AgentGit');
  const setup = await realpath(process.env.AGENTGIT_INSTALL_SETUP!);
  expect(basename(setup)).toBe('AgentGitSetup.exe');
  expect(digest(await readFile(setup))).toBe(process.env.AGENTGIT_INSTALL_SETUP_SHA256);
  // MSIX 的真实目录必须由操作者显式固定，不能自动接受任意重定向。
  const ownRoot = async () => { expect((await realpath(root)).toLowerCase()).toBe(physicalRoot.toLowerCase()); };
  await ownRoot();
  expect(await exists(join(root, 'app-0.1.0/AgentGit.exe'))).toBe(true);
  expect(await exists(join(profile, 'preferences.json')), '不得覆盖操作者已有桌面偏好').toBe(false);
  expect(inside(physicalRoot, await realpath(profile))).toBe(false);
  const evidence = await mkdtemp(join(tmpdir(), 'agentgit-install-qa-'));
  const backend = await startBackend();
  const env: Record<string, string> = { ...Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined)),
    AGENTGIT_API_TOKEN: TOKEN, AGENTGIT_API_URL: backend.url };
  for (const key of Object.keys(env)) if (['path', 'node_path', 'node_options', 'agentgit_desktop_profile_dir', 'openai_api_key'].includes(key.toLowerCase())) delete env[key];
  env.PATH = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32');
  const launch = (version: string) => electron.launch({ executablePath: join(root, `app-${version}`, 'AgentGit.exe'), args: [], cwd: evidence, env });
  const cli = async (binary: string, args: string[], expected = [0]) => {
    const child = spawn(binary, args, { env, cwd: evidence, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = ''; for (const stream of [child.stdout, child.stderr]) stream.on('data', bytes => { output = (output + String(bytes)).slice(-8000); });
    const [code] = await once(child, 'exit'); expect(expected, output).toContain(code); return { code, output };
  };
  const reg = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32/reg.exe');
  const registry = ['query', 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\AgentGit', '/v', 'DisplayVersion'];
  let desktop: Awaited<ReturnType<typeof launch>> | undefined;
  try {
    expect(inside(physicalRoot, await realpath(backend.root))).toBe(false);
    expect((await cli(reg, registry)).output).toContain('0.1.0');
    desktop = await launch('0.1.0'); let page = await desktop.firstWindow();
    expect(await desktop.evaluate(({ app }) => app.getPath('userData'))).toBe(profile);
    expect(await desktop.evaluate(({ app }) => app.getVersion())).toBe('0.1.0');
    await page.getByLabel('新项目名称').fill('安装升级持久数据验证');
    await page.getByRole('button', { name: '创建并选择', exact: true }).click();
    await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '新建运行', exact: true }).click();
    await page.getByRole('button', { name: '提交运行', exact: true }).click();
    await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    const source = (await page.locator('.page-heading code').innerText()).trim();
    await completed(page, source);
    const repo = String(data(await page.evaluate(() => window.agentgit.getConnection())).repositoryId);
    const args = { repository: repo, source_run: source };
    const baseline = await backend.command('storage_audit', args);
    const full = await backend.command('audit', args);
    expect(baseline.schema).toBe('0012_replay_source_index'); expect(baseline.worker_alive).toBe(true);
    expect(full.source_tools).toHaveLength(5);
    const posts = await backend.command('traffic');
    await desktop.close(); desktop = undefined;
    const preferences = JSON.parse(await readFile(join(profile, 'preferences.json'), 'utf8'));
    expect(preferences.repositoryId).toBe(repo);
    await ownRoot();
    await cli(setup, ['--silent']);
    const installLog = await readFile(join(process.env.LOCALAPPDATA!, 'SquirrelTemp/Squirrel-Install.log'), 'utf8');
    expect(installLog).toContain(root); expect(installLog).toContain('app-0.1.1');
    await writeFile(join(evidence, 'upgrade.log'), installLog);
    await copyFile(join(root, 'Squirrel-Shortcut.log'), join(evidence, 'shortcut.log'));
    expect((await cli(reg, registry)).output).toContain('0.1.1');
    desktop = await launch('0.1.1'); page = await desktop.firstWindow();
    expect(await desktop.evaluate(({ app }) => app.getVersion())).toBe('0.1.1');
    await expect(page.locator('footer')).toContainText('AgentGit 0.1.1');
    await expect(page.getByRole('button', { name: '查看时间线 →', exact: true })).toBeVisible();
    expect(data(await page.evaluate(() => window.agentgit.getConnection())).repositoryId).toBe(repo);
    expect(await backend.command('storage_audit', args)).toEqual(baseline);
    expect(await backend.command('traffic')).toEqual(posts);
    await page.screenshot({ path: join(evidence, 'upgraded.png'), fullPage: true });
    await desktop.close(); desktop = undefined;
    const uninstall = async () => {
      await ownRoot();
      const updater = await realpath(join(root, 'Update.exe'));
      expect(inside(physicalRoot, updater)).toBe(true);
      await cli(updater, ['--uninstall', '--silent']);
      await expect.poll(() => exists(join(root, 'app-0.1.1/AgentGit.exe'))).toBe(false);
      expect((await cli(reg, registry, [1])).code).toBe(1);
      expect(await exists(join(process.env.APPDATA!, 'Microsoft/Windows/Start Menu/Programs/AgentGit/AgentGit.lnk'))).toBe(false);
      const retained = JSON.parse(await readFile(join(profile, 'preferences.json'), 'utf8'));
      expect(retained.repositoryId).toBe(preferences.repositoryId); expect(retained.baseUrl).toBe(preferences.baseUrl);
    };
    await uninstall();
    const afterUninstall = await backend.command('storage_audit', args);
    expect(afterUninstall).toEqual(baseline);
    expect((await fetch(`${backend.url}/health`)).status).toBe(200);
    expect(await backend.command('traffic')).toEqual(posts);
    await cli(setup, ['--silent']);
    desktop = await launch('0.1.1'); page = await desktop.firstWindow();
    await expect(page.getByRole('button', { name: '查看时间线 →', exact: true })).toBeVisible();
    expect(objects(data(await page.evaluate(() => window.agentgit.listRuns({}))).items)).toHaveLength(1);
    expect(await backend.command('storage_audit', args)).toEqual(baseline);
    expect(await backend.command('traffic')).toEqual(posts);
    await page.getByRole('button', { name: '新建运行', exact: true }).click();
    await page.getByLabel('启用固定五工具').uncheck();
    await page.getByRole('button', { name: '提交运行', exact: true }).click();
    await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    const next = (await page.locator('.page-heading code').innerText()).trim();
    expect(next).not.toBe(source); await completed(page, next);
    await expect(page.locator('.page-heading').getByText('已完成', { exact: true })).toBeVisible();
    const afterNew = await backend.command('storage_audit', args);
    expect(afterNew.source_fingerprint).toBe(baseline.source_fingerprint);
    expect(afterNew.schema).toBe(baseline.schema); expect(afterNew.worker_alive).toBe(true);
    expect((await backend.command('traffic')).posts).toHaveLength((posts.posts as unknown[]).length + 3);
    await page.screenshot({ path: join(evidence, 'reinstalled-completed.png'), fullPage: true });
    await desktop.close(); desktop = undefined;
    await uninstall();
    const afterFinalUninstall = await backend.command('storage_audit', args);
    expect(afterFinalUninstall).toEqual(afterNew);
    await writeFile('../.agentgit/evidence/S6-install-lifecycle-qa.json', JSON.stringify({ root, physicalRoot, profile, physicalProfile: await realpath(profile), evidence, setup, setup_sha256: digest(await readFile(setup)),
      repository: repo, source, next, baseline, afterUninstall, afterNew, afterFinalUninstall,
      upgrade_log_sha256: digest(Buffer.from(installLog)), postsBeforeUpgrade: posts.posts, application_uninstalled: true,
      scope: '当前 Windows 用户；真实安装/升级/卸载，不满足独立 Windows 用户条件；后端测试 Schema 仅在证据保存后由 fixture 清理' }, null, 2));
  } finally { try { if (desktop) await desktop.close(); } finally { await backend.stop(); } }
});
