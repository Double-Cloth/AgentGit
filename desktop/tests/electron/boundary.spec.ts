import { test, expect, _electron as electron } from '@playwright/test';
import { createServer, type IncomingMessage } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const TOKEN = 'fixture-electron-token-00000000000000000000';
const REPO = '00000000-0000-4000-8000-000000000001';
const RUN = '00000000-0000-4000-8000-000000000002';
const COMMIT = 'a'.repeat(64);
const PARENT = 'b'.repeat(64);
const EVIDENCE = process.env.AGENTGIT_ELECTRON_EVIDENCE_PREFIX ?? 'S6';
if (!/^[a-zA-Z0-9_-]{1,80}$/.test(EVIDENCE)) throw new Error('测试证据前缀无效');

test('连接降级：API 未监听、数据库 503、版本不兼容、身份拒绝及恢复', async () => {
  let mode = 'healthy';
  const methods: string[] = [];
  const server = createServer((request, response) => {
    methods.push(request.method!);
    response.setHeader('Content-Type', 'application/json');
    if (request.url === '/health') response.end(JSON.stringify({ status: 'ok', version: mode === 'version' ? '0.2.0' : '0.1.0' }));
    else if (request.url === '/ready') {
      if (mode === 'database') { response.writeHead(503); response.end(JSON.stringify({ error: { code: 'database_unavailable', message: '数据库不可用' } })); }
      else response.end(JSON.stringify({ status: 'ready' }));
    } else if (request.url === '/capabilities' && mode !== 'identity') {
      response.end(JSON.stringify({ schema_version: 1, version: '0.1.0', features: ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff'] }));
    } else { response.writeHead(401); response.end(JSON.stringify({ error: { code: 'unauthorized', message: '本地身份验证失败，请核对预置令牌' } })); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const address = server.address(); if (!address || typeof address === 'string') throw new Error('连接测试端点不可用');
  await new Promise<void>(resolve => server.close(() => resolve()));
  const evidence = await mkdtemp(join(tmpdir(), 'agentgit-connection-qa-'));
  const executable = process.env.AGENTGIT_DESKTOP_EXECUTABLE;
  const desktop = await electron.launch({ ...(executable ? { executablePath: resolve(executable), args: [], cwd: evidence } : { args: ['.'], cwd: resolve('.') }),
    env: { ...process.env, AGENTGIT_API_TOKEN: TOKEN, AGENTGIT_API_URL: `http://127.0.0.1:${address.port}`, AGENTGIT_DESKTOP_PROFILE_DIR: join(evidence, 'profile') } });
  try {
    const page = await desktop.firstWindow(), errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); });
    await expect(page).toHaveTitle('AgentGit'); expect(page.url()).toBe('agentgit://app/index.html');
    const message = page.locator('.connection-message');
    await expect(message).toContainText('无法连接本地 API，请检查服务地址与启动状态');
    await page.getByLabel('新项目名称').fill('连接恢复测试'); await page.getByLabel('已有项目 ID').fill(REPO);
    const create = page.getByRole('button', { name: '创建并选择', exact: true }), select = page.getByRole('button', { name: '选择项目', exact: true });
    await expect(create).toBeDisabled(); await expect(select).toBeDisabled();
    await page.screenshot({ path: join(evidence, 'api-unavailable.png'), fullPage: true });
    server.listen(address.port, '127.0.0.1'); await once(server, 'listening');
    for (const [state, text] of [['database', 'API 已连接，数据库尚未就绪'], ['version', '后端版本不兼容'], ['identity', '本地身份验证失败']]) {
      mode = state!; await page.getByRole('button', { name: '重新检查', exact: true }).click();
      await expect(message).toContainText(text!); await expect(create).toBeDisabled(); await expect(select).toBeDisabled();
      await expect(page.getByText('未知 / 未探测', { exact: true })).toBeVisible();
      await page.screenshot({ path: join(evidence, `${state}.png`), fullPage: true });
    }
    mode = 'healthy'; await page.getByRole('button', { name: '重新检查', exact: true }).click();
    await expect(message).toContainText('API 与数据库可用；Worker 和执行环境仍须以实际任务结果确认');
    await expect(create).toBeEnabled(); await expect(select).toBeEnabled();
    await expect(page.getByText('未知 / 未探测', { exact: true })).toBeVisible();
    await page.screenshot({ path: join(evidence, 'recovered.png'), fullPage: true });
    expect(methods.every(method => method === 'GET')).toBe(true); expect(errors).toEqual([]);
    await writeFile(`../.agentgit/evidence/${EVIDENCE}-connection-qa.json`, JSON.stringify({ evidence, executable: executable ?? null, methods, errors,
      cases: ['实际未监听端口', 'HTTP fixture 数据库 503', 'HTTP fixture 版本不兼容', 'HTTP fixture 401', '恢复且无修改请求'],
      workerAndDocker: '始终未知；未在本测试停机、移除镜像或声称已就绪' }, null, 2));
  } finally {
    try { await desktop.close(); }
    finally { server.closeAllConnections(); if (server.listening) await new Promise<void>(resolve => server.close(() => resolve())); }
  }
});

test('真实 Electron：本地资源、隔离桥接、四页查询及受控提交', async () => {
  const requests: IncomingMessage[] = [], bodies: Record<string, unknown>[] = [];
  let runState = 'running';
  let approvalPending = false;
  const config = { schema_version: 1, kind: 'offline_research_agent', model: 'mock-research/v1', tools: ['corpus_search', 'source_read', 'artifact_write'], limits: { schema_version: 1, max_steps: 32, max_model_calls: 32, max_tool_calls: 16 } };
  const server = createServer((request, response) => {
    requests.push(request); const path = new URL(request.url!, 'http://localhost').pathname;
    response.setHeader('Content-Type', 'application/json');
    if (request.method === 'POST') {
      const chunks: Buffer[] = []; request.on('data', chunk => chunks.push(chunk));
      request.on('end', () => { bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>);
        if (path.endsWith('/decision')) approvalPending = false;
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
      : path === `/repositories/${REPO}` ? { id: REPO, name: '桌面测试', config_version: 0 }
      : path.endsWith('/branches') ? { items: [{ name: 'main', version: 1, head_commit_id: COMMIT }], has_more: false }
      : path.endsWith('/commits') ? { items: [{ commit_id: COMMIT, parents: [PARENT], created_at: '2026-10-22T00:00:00Z' }, { commit_id: PARENT, parents: [] }], has_more: false }
      : path === `/commits/${COMMIT}` || path === `/commits/${PARENT}` ? { commit_id: path.slice('/commits/'.length), manifest: {}, snapshot: { data: { status: 'completed', checkpoint: path === `/commits/${PARENT}` ? 'parent' : 'head', injected: '<img src=x onerror="window.injected=true">' } }, environment: {} }
      : path.endsWith('/runs') ? { items: [run], next_after_id: null }
      : path.endsWith('/approvals') ? { items: approvalPending ? [{ id: REPO, version: 3, status: 'pending', expires_at: '2099-01-01T00:00:00Z', request: { tool: 'source_read' } }] : [] }
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
  const executable = process.env.AGENTGIT_DESKTOP_EXECUTABLE;
  const desktop = await electron.launch({ ...(executable ? { executablePath: resolve(executable), args: [], cwd: profile } : { args: ['.'], cwd: resolve('.') }), env: { ...process.env,
    AGENTGIT_API_TOKEN: TOKEN, OPENAI_API_KEY: 'sk-public-desktop-env-boundary-fixture', AGENTGIT_API_URL: `http://127.0.0.1:${address.port}`, AGENTGIT_DESKTOP_PROFILE_DIR: profile } });
  try {
    const page = await desktop.firstWindow();
    expect(await desktop.evaluate(() => process.env.OPENAI_API_KEY)).toBeUndefined();
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    await expect(page).toHaveTitle('AgentGit'); expect(page.url()).toBe('agentgit://app/index.html');
    if (executable) expect(await desktop.evaluate(({ app }) => app.isPackaged)).toBe(true);
    const desktopVersion = await desktop.evaluate(({ app }) => app.getVersion());
    await expect(page.locator('footer')).toContainText(`AgentGit ${desktopVersion}`);
    await expect(page.getByRole('heading', { name: '连接与项目' })).toBeVisible();
    await page.getByText('版本与开源授权', { exact: true }).click();
    await expect(page.getByText(`AgentGit ${desktopVersion} · Windows x64`, { exact: true })).toBeVisible();
    await expect(page.getByText('AgentGit 第三方软件授权说明', { exact: false })).toBeVisible();
    await page.getByText('版本与开源授权', { exact: true }).click();
    await expect(page.getByText('API 与数据库可用；Worker')).toBeVisible();
    expect(await page.evaluate(() => ({ require: typeof (globalThis as { require?: unknown }).require,
      process: typeof (globalThis as { process?: unknown }).process, bridgeKeys: Object.keys(window.agentgit) }))).toMatchObject({ require: 'undefined', process: 'undefined' });
    expect(await page.evaluate(() => Object.keys(window.agentgit))).toEqual(expect.arrayContaining([
      'freezeValidator', 'getValidator', 'createExperiment', 'getExperiment', 'getInterventionContext', 'createIntervention', 'getIntervention',
    ]));
    const preferences = await desktop.evaluate(({ BrowserWindow }) => {
      // 固定 Electron 运行时的测试观测方法未列入公开类型；产品代码不依赖它。
      const contents = BrowserWindow.getAllWindows()[0]!.webContents as unknown as { getLastWebPreferences: () => Electron.WebPreferences };
      return contents.getLastWebPreferences();
    });
    expect(preferences).toMatchObject({ nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false });
    expect(await desktop.evaluate(() => process.env.AGENTGIT_API_TOKEN)).toBeUndefined();
    await page.getByLabel('已有项目 ID').fill(REPO); await page.getByRole('button', { name: '选择项目', exact: true }).click();
    await expect(page.getByText('Electron 边界测试运行')).toBeVisible();
    expect(await page.evaluate(() => window.agentgit.getExperiment({ experimentId: '../private' }))).toMatchObject({ ok: false, error: { code: 'validation' } });
    expect(requests.some(request => request.url?.startsWith('/experiments/'))).toBe(false);
    await page.getByRole('button', { name: '收起', exact: true }).click();
    for (const width of [1440, 1024, 768, 360]) {
      await page.setViewportSize({ width, height: 900 });
      const layout = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth,
        overflow: [...document.querySelectorAll('body *')].filter(element => element.getBoundingClientRect().right > document.documentElement.clientWidth && !element.closest('.table-scroll')).slice(0, 10).map(element => ({ tag: element.tagName, class: element.className, text: element.textContent?.slice(0, 80), right: element.getBoundingClientRect().right })) }));
      await page.screenshot({ path: `../.agentgit/evidence/${EVIDENCE}-runs-${width}.png`, fullPage: true });
      expect(layout.scroll, JSON.stringify({ requestedWidth: width, ...layout })).toBeLessThanOrEqual(layout.width);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: `../.agentgit/evidence/${EVIDENCE}-runs-electron.png`, fullPage: true });
    await page.getByRole('button', { name: '查看时间线 →' }).click();
    await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    await expect(page.getByLabel('检查点关系图')).toBeVisible();
    await page.getByRole('tab', { name: '逻辑状态', exact: true }).click();
    const parentNode = page.getByRole('group', { name: `检查点 ${PARENT}`, exact: true });
    for (let step = 0; step < 80 && !(await parentNode.evaluate(element => element === document.activeElement)); step++) await page.keyboard.press('Tab');
    expect(await parentNode.evaluate(element => element === document.activeElement)).toBe(true);
    expect(await parentNode.evaluate(element => ({ width: getComputedStyle(element).outlineWidth, color: getComputedStyle(element).outlineColor,
      style: getComputedStyle(element).outlineStyle, offset: getComputedStyle(element).outlineOffset, visible: element.matches(':focus-visible') })))
      .toEqual({ width: '2px', color: 'rgb(20, 20, 19)', style: 'solid', offset: '2px', visible: true });
    await page.keyboard.press('Enter');
    await expect(page.locator('.inspector .section-heading code')).toHaveText(PARENT.slice(0, 12));
    await expect(page.locator('.inspector pre').first()).toContainText('"checkpoint": "parent"');
    await page.getByLabel('检查点关系图').screenshot({ path: join(profile, 'graph-keyboard.png') });
    const headNode = page.getByRole('group', { name: `检查点 ${COMMIT}`, exact: true });
    for (let step = 0; step < 10 && !(await headNode.evaluate(element => element === document.activeElement)); step++) await page.keyboard.press('Tab');
    expect(await headNode.evaluate(element => element === document.activeElement)).toBe(true);
    await page.keyboard.press('Space');
    await expect(page.locator('.inspector .section-heading code')).toHaveText(COMMIT.slice(0, 12));
    await expect(page.locator('.inspector pre').first()).toContainText('"checkpoint": "head"');
    await parentNode.click();
    await expect(page.locator('.inspector pre').first()).toContainText('"checkpoint": "parent"');
    await page.getByRole('group', { name: `检查点 ${COMMIT}`, exact: true }).click();
    await expect(page.locator('.inspector .section-heading code')).toHaveText(COMMIT.slice(0, 12));
    await page.getByRole('tab', { name: '执行证据', exact: true }).click();
    await writeFile(`../.agentgit/evidence/${EVIDENCE}-keyboard-graph-qa.json`, JSON.stringify({ screenshot: join(profile, 'graph-keyboard.png'),
      selected: PARENT, keys: ['Tab', 'Enter', 'Space'], desktop: desktopVersion, executable: executable ?? null }, null, 2));
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
      await page.screenshot({ path: `../.agentgit/evidence/${EVIDENCE}-timeline-${width}.png`, fullPage: true });
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: `../.agentgit/evidence/${EVIDENCE}-timeline-electron.png`, fullPage: true });
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
    await page.screenshot({ path: `../.agentgit/evidence/${EVIDENCE}-branch-electron.png`, fullPage: true });
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
    await page.screenshot({ path: `../.agentgit/evidence/${EVIDENCE}-diff-electron.png`, fullPage: true });
    await page.getByRole('button', { name: '运行', exact: true }).click();
    await page.getByRole('button', { name: '新建运行', exact: true }).click();
    await page.getByRole('button', { name: '提交运行', exact: true }).dblclick();
    await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
    expect(requests.filter(request => request.method === 'POST')).toHaveLength(3);
    expect(bodies[2]).toMatchObject({ schema_version: 1, expected_head: COMMIT, expected_branch_version: 1, branch_name: 'main' });
    approvalPending = true; runState = 'waiting_approval';
    await expect(page.getByRole('button', { name: '批准此动作', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '批准此动作', exact: true }).dblclick();
    await expect(page.getByRole('button', { name: '批准此动作', exact: true })).toHaveCount(0);
    expect(requests.filter(request => request.method === 'POST' && request.url?.endsWith('/decision'))).toHaveLength(1);
    expect(bodies.at(-1)).toMatchObject({ decision: 'approve', expected_version: 3 });
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
  } finally {
    try { await desktop.close(); }
    finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
  }
});
