import { expect, _electron as electron, type ElectronApplication, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';

export const EVIDENCE = process.env.AGENTGIT_ELECTRON_EVIDENCE_PREFIX ?? 'S7-desktop';
if (!/^[a-zA-Z0-9_-]{1,80}$/.test(EVIDENCE)) throw new Error('测试证据前缀无效');

export function launchDesktop(profile: string, url: string, token: string) {
  const executable = process.env.AGENTGIT_DESKTOP_EXECUTABLE;
  const env: Record<string, string> = Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined));
  if (executable) {
    // 打包进程使用独立目录和系统 PATH，不借用构建用 Node/npm 或其注入选项。
    for (const key of Object.keys(env)) if (['path', 'node_path', 'node_options'].includes(key.toLowerCase())) delete env[key];
    env.PATH = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32');
  }
  delete env.AGENTGIT_DESKTOP_EXECUTABLE;
  Object.assign(env, { AGENTGIT_API_TOKEN: token, AGENTGIT_API_URL: url, AGENTGIT_DESKTOP_PROFILE_DIR: profile });
  return electron.launch({ ...(executable ? { executablePath: resolve(executable), args: [], cwd: dirname(profile) } : { args: ['.'], cwd: resolve('.') }), env });
}

export async function desktopIdentity(desktop: ElectronApplication, page: Page) {
  const runtime = await desktop.evaluate(({ app }) => ({ version: app.getVersion(), packaged: app.isPackaged, executable: process.execPath, platform: process.platform, arch: process.arch, path: process.env.PATH, versions: process.versions }));
  const manifest = JSON.parse(await readFile(resolve('package.json'), 'utf8')) as { version: string };
  expect(runtime.version).toBe(manifest.version);
  expect(runtime.platform).toBe('win32'); expect(runtime.arch).toBe('x64');
  await expect(page).toHaveTitle('AgentGit'); expect(page.url()).toBe('agentgit://app/index.html');
  await expect(page.locator('footer')).toContainText(`AgentGit ${runtime.version}`);
  if (process.env.AGENTGIT_DESKTOP_EXECUTABLE) {
    expect(runtime.packaged).toBe(true);
    expect(runtime.executable.toLowerCase()).toBe(resolve(process.env.AGENTGIT_DESKTOP_EXECUTABLE).toLowerCase());
    expect(runtime.path).toBe(join(process.env.SystemRoot ?? 'C:\\Windows', 'System32'));
  }
  return runtime;
}
