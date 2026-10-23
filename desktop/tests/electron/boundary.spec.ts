import { test, expect, _electron as electron } from '@playwright/test';
import { createServer, type IncomingMessage } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const TOKEN = 'fixture-electron-token-00000000000000000000';
const REPO = '00000000-0000-4000-8000-000000000001';
const RUN = '00000000-0000-4000-8000-000000000002';
const COMMIT = 'a'.repeat(64);

test('真实 Electron：本地资源、隔离桥接、四页查询及受控提交', async () => {
  const requests: IncomingMessage[] = [], bodies: Record<string, unknown>[] = [];
  let runState = 'running';
  const config = { schema_version: 1, kind: 'offline_research_agent', model: 'mock-research/v1', tools: ['corpus_search', 'source_read', 'artifact_write'], limits: { schema_version: 1, max_steps: 32, max_model_calls: 32, max_tool_calls: 16 } };
  const server = createServer((request, response) => {
    requests.push(request); const path = new URL(request.url!, 'http://localhost').pathname;
    response.setHeader('Content-Type', 'application/json');
    if (request.method === 'POST') {
      const chunks: Buffer[] = []; request.on('data', chunk => chunks.push(chunk));
      request.on('end', () => { bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>);
        response.end(JSON.stringify(path === '/agents' ? { config_id: REPO, version: 1 } : path === '/tasks' ? { task_id: REPO } : { run_id: RUN, job_id: REPO })); }); return;
    }
    if (path.endsWith('/content/code') || path.endsWith('/content/stdout')) {
      const content = Buffer.from(path.endsWith('/code') ? 'claims_per_framework' : 'statistics written');
      response.setHeader('ETag', `"${COMMIT}"`); response.setHeader('X-Content-Bytes', String(content.length));
      response.setHeader('X-Content-Offset', '0'); response.setHeader('X-Next-Offset', ''); response.end(content); return;
    }
    const run = { id: RUN, repository_id: REPO, branch_name: 'main', mode: 'standard', status: runState, state_version: 2, checkpoint_commit_id: COMMIT, goal: { text: 'Electron 边界测试运行' }, created_at: '2026-10-22T00:00:00Z' };
    if (path === '/diff') {
      const query = new URL(request.url!, 'http://localhost').searchParams;
      if (!query.has('left_commit') || !query.has('right_commit')) {
        response.writeHead(422); response.end(JSON.stringify({ error: { code: 'validation', message: '缺少 Commit 参数' } })); return;
      }
    }
    const result = path === '/health' ? { status: 'ok', version: '0.1.0' } : path === '/ready' ? { status: 'ready' }
      : path === '/capabilities' ? { schema_version: 1, version: '0.1.0', features: ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff'], default_config: config, execution_config: config }
      : path === `/repositories/${REPO}` ? { id: REPO, name: '桌面测试' }
      : path.endsWith('/branches') ? { items: [{ name: 'main', version: 1, head_commit_id: COMMIT }], has_more: false }
      : path.endsWith('/commits') ? { items: [{ commit_id: COMMIT, parents: [], created_at: '2026-10-22T00:00:00Z' }], has_more: false }
      : path === `/commits/${COMMIT}` ? { commit_id: COMMIT, manifest: {}, snapshot: { data: { status: 'completed', injected: '<img src=x onerror="window.injected=true">' } }, environment: {} }
      : path.endsWith('/runs') ? { items: [run], next_after_id: null }
      : path.endsWith('/executions') ? { items: [{ id: REPO, run_id: RUN, provider: 'tool:code_exec/v1', logical_step_id: 'code_exec', attempt: 1 }], next_after_id: null }
      : path.endsWith(`/executions/${REPO}`) ? { run_id: RUN, physical_run_id: RUN, physical_call_id: REPO, execution_status: 'complete', cleanup_confirmed: true, wall_ms: 12, resources: ['code', 'stdout'], result: { schema_version: 1, exit_code: 0 } }
      : path === `/runs/${RUN}` ? run
      : path === '/diff' ? { items: [{ path: '/state/value', operation: 'replace', before: '旧值', after: '新值' }], total_changes: 1, common_ancestor: COMMIT, has_more: false, next_after: 1 }
      : { items: [], next_after_seq: 0, next_after_id: null };
    response.end(JSON.stringify(result));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const address = server.address(); if (!address || typeof address === 'string') throw new Error('测试端点不可用');
  const profile = await mkdtemp(join(tmpdir(), 'agentgit-electron-'));
  const desktop = await electron.launch({ args: ['.'], cwd: resolve('.'), env: { ...process.env,
    AGENTGIT_API_TOKEN: TOKEN, AGENTGIT_API_URL: `http://127.0.0.1:${address.port}`, AGENTGIT_DESKTOP_PROFILE_DIR: profile } });
  try {
    const page = await desktop.firstWindow();
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    await expect(page).toHaveTitle('AgentGit'); expect(page.url()).toBe('agentgit://app/index.html');
    await expect(page.getByRole('heading', { name: '连接与项目' })).toBeVisible();
    await expect(page.getByText('API 与数据库可用；Worker')).toBeVisible();
    expect(await page.evaluate(() => ({ require: typeof (globalThis as { require?: unknown }).require,
      process: typeof (globalThis as { process?: unknown }).process, bridgeKeys: Object.keys(window.agentgit) }))).toMatchObject({ require: 'undefined', process: 'undefined' });
    const preferences = await desktop.evaluate(({ BrowserWindow }) => {
      // 固定 Electron 运行时的测试观测方法未列入公开类型；产品代码不依赖它。
      const contents = BrowserWindow.getAllWindows()[0]!.webContents as unknown as { getLastWebPreferences: () => Electron.WebPreferences };
      return contents.getLastWebPreferences();
    });
    expect(preferences).toMatchObject({ nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false });
    expect(await desktop.evaluate(() => process.env.AGENTGIT_API_TOKEN)).toBeUndefined();
    await page.getByLabel('已有项目 ID').fill(REPO); await page.getByRole('button', { name: '选择项目', exact: true }).click();
    await expect(page.getByText('Electron 边界测试运行')).toBeVisible();
    await page.getByRole('button', { name: '收起', exact: true }).click();
    for (const width of [1440, 1024, 768, 360]) {
      await page.setViewportSize({ width, height: 900 });
      const layout = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth,
        overflow: [...document.querySelectorAll('body *')].filter(element => element.getBoundingClientRect().right > document.documentElement.clientWidth && !element.closest('.table-scroll')).slice(0, 10).map(element => ({ tag: element.tagName, class: element.className, text: element.textContent?.slice(0, 80), right: element.getBoundingClientRect().right })) }));
      await page.screenshot({ path: `../.agentgit/evidence/S6-runs-${width}.png`, fullPage: true });
      expect(layout.scroll, JSON.stringify({ requestedWidth: width, ...layout })).toBeLessThanOrEqual(layout.width);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: '../.agentgit/evidence/S6-runs-electron.png', fullPage: true });
    await page.getByRole('button', { name: '查看时间线 →' }).click();
    await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    await expect(page.getByLabel('检查点关系图')).toBeVisible();
    await expect(page.getByText('statistics written', { exact: true })).toBeVisible();
    await page.getByLabel('原始证据').selectOption('code');
    await expect(page.getByText('claims_per_framework', { exact: true })).toBeVisible();
    const reads = requests.filter(request => request.url === `/runs/${RUN}`).length;
    runState = 'completed';
    await expect.poll(() => requests.filter(request => request.url === `/runs/${RUN}`).length).toBeGreaterThanOrEqual(reads + 2);
    await expect(page.getByLabel('原始证据')).toHaveValue('code');
    await expect(page.getByText('claims_per_framework', { exact: true })).toBeVisible();
    await page.getByRole('tab', { name: '逻辑状态' }).click();
    await expect(page.getByText('window.injected=true', { exact: false })).toBeVisible();
    expect(await page.evaluate(() => (window as unknown as { injected?: boolean }).injected)).toBeUndefined();
    for (const width of [768, 360]) {
      await page.setViewportSize({ width, height: 900 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      await page.screenshot({ path: `../.agentgit/evidence/S6-timeline-${width}.png`, fullPage: true });
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: '../.agentgit/evidence/S6-timeline-electron.png', fullPage: true });
    await page.getByRole('button', { name: '从所选检查点分支 →' }).click();
    await expect(page.getByRole('heading', { name: '分支实验', exact: true })).toBeVisible();
    await expect(page.getByLabel('安全起点 Commit')).toHaveValue(COMMIT);
    await expect(page.getByLabel('验证终点 Commit')).toBeVisible();
    await page.getByRole('radio', { name: /Live/ }).check();
    await expect(page.getByLabel('验证终点 Commit')).toHaveCount(0);
    await expect(page.getByLabel('干预 overrides JSON')).toBeVisible();
    for (const width of [768, 360]) {
      await page.setViewportSize({ width, height: 900 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: '../.agentgit/evidence/S6-branch-electron.png', fullPage: true });
    await page.getByRole('button', { name: '差异', exact: true }).click();
    await page.getByLabel('BASE · 基线 Commit', { exact: true }).fill(COMMIT);
    await page.getByLabel('HEAD · 对照 Commit', { exact: true }).fill('b'.repeat(64));
    await page.getByRole('button', { name: '比较', exact: true }).click();
    await expect(page.getByText('1 项差异', { exact: true })).toBeVisible();
    await expect(page.getByText('"旧值"', { exact: true })).toBeVisible(); await expect(page.getByText('"新值"', { exact: true })).toBeVisible();
    for (const width of [768, 360]) {
      await page.setViewportSize({ width, height: 900 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: '../.agentgit/evidence/S6-diff-electron.png', fullPage: true });
    await page.getByRole('button', { name: '运行', exact: true }).click();
    await page.getByRole('button', { name: '新建运行', exact: true }).click();
    await page.getByRole('button', { name: '提交运行', exact: true }).dblclick();
    await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    expect(requests.filter(request => request.method === 'POST')).toHaveLength(3);
    expect(bodies[2]).toMatchObject({ schema_version: 1, expected_head: COMMIT, expected_branch_version: 1, branch_name: 'main' });
    const boundary = await page.evaluate(async () => window.agentgit.listRuns({ repositoryId: 'foreign' } as never));
    expect(boundary).toMatchObject({ ok: false, error: { code: 'validation' } });
    const forbidden = await desktop.evaluate(async ({ BrowserWindow }, preload) => {
      const hostile = new BrowserWindow({ show: false, webPreferences: { preload, nodeIntegration: false, contextIsolation: true, sandbox: true } });
      try { await hostile.loadURL('agentgit://app/index.html');
        return await hostile.webContents.executeJavaScript('window.agentgit.getConnection()'); }
      finally { hostile.destroy(); }
    }, resolve('.build/preload/index.js'));
    expect(forbidden).toMatchObject({ ok: false, error: { code: 'forbidden' } });
    const opened = await page.evaluate(() => { const value = window.open('https://example.com'); return value === null; });
    expect(opened).toBe(true); expect(desktop.windows()).toHaveLength(1);
    await page.evaluate(() => { window.location.href = 'https://example.com'; });
    await expect.poll(() => desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.webContents.getURL())).toBe('agentgit://app/index.html');
    expect(await page.evaluate(() => location.href)).toBe('agentgit://app/index.html');
    const rendered = await page.locator('body').innerText(); expect(rendered).not.toContain(TOKEN);
    for (const request of requests.filter(request => !['/health', '/ready'].includes(request.url!))) {
      expect(request.headers.authorization).toBe(`Bearer ${TOKEN}`); expect(request.headers.origin).toBeUndefined();
    }
    expect(await readFile(join(profile, 'preferences.json'), 'utf8')).not.toContain(TOKEN);
    expect(errors).toEqual([]);
  } finally { await desktop.close(); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
});
