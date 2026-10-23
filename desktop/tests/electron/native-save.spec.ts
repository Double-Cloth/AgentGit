import { test, expect, _electron as electron } from '@playwright/test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, realpath, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const REPO = '00000000-0000-4000-8000-000000000001', RUN = '00000000-0000-4000-8000-000000000002';
const FILE = '00000000-0000-4000-8000-000000000003', COMMIT = 'a'.repeat(64);
const TOKEN = 'fixture-native-save-token-00000000000000000000';
test('原生保存短流程：包内 Timeline 所选产物 → 实际保存与取消 → 原字节和无额外文件', async () => {
  expect(process.env.AGENTGIT_NATIVE_SAVE).toBe('1');
  const executable = await realpath(process.env.AGENTGIT_DESKTOP_EXECUTABLE!);
  const evidence = await realpath(await mkdtemp(join(tmpdir(), 'agentgit-native-short-')));
  const output = join(evidence, 'exported-version.txt');
  const bytes = Buffer.from('所选历史版本的原始内容。\n<文本不作为代码运行>\n', 'utf8');
  const object = createHash('sha256').update(`agentgit\0AGJSON-v1\0blob\0${bytes.length}\0`).update(bytes).digest('hex');
  const server = createServer((request, response) => {
    const path = new URL(request.url!, 'http://localhost').pathname;
    response.setHeader('Content-Type', 'application/json');
    if (request.method !== 'GET') { response.writeHead(405); response.end('{}'); return; }
    if (!['/health', '/ready'].includes(path) && request.headers.authorization !== `Bearer ${TOKEN}`) { response.writeHead(401); response.end('{}'); return; }
    if (path === `/artifacts/${FILE}/content`) {
      response.setHeader('Content-Type', 'text/plain'); response.setHeader('ETag', `"${object}"`);
      response.setHeader('Content-Disposition', "attachment; filename*=UTF-8''native-version.txt"); response.end(bytes); return;
    }
    const run = { id: RUN, repository_id: REPO, branch_name: 'main', mode: 'standard', status: 'completed', state_version: 1, checkpoint_commit_id: COMMIT, goal: { text: '原生保存专用 HTTP fixture' } };
    response.end(JSON.stringify(path === '/health' ? { status: 'ok', version: '0.1.0' } : path === '/ready' ? { status: 'ready' }
      : path === '/capabilities' ? { schema_version: 1, version: '0.1.0', features: ['run_pagination', 'historical_artifacts', 'execution_evidence', 'recorded', 'live', 'structural_diff'] }
      : path === `/repositories/${REPO}` ? { id: REPO, config_version: 0 }
      : path.endsWith('/branches') ? { items: [{ name: 'main', version: 1, head_commit_id: COMMIT }] }
      : path.endsWith('/commits') ? { items: [{ commit_id: COMMIT, parents: [] }] }
      : path === `/commits/${COMMIT}/artifacts` ? { items: [{ id: FILE, relative_path: 'native-version.txt', object_id: object }] }
      : path === `/commits/${COMMIT}` ? { manifest: {}, snapshot: { data: { status: 'completed' } }, environment: {} }
      : path === `/runs/${RUN}` ? run : path.endsWith('/runs') ? { items: [run] } : { items: [], next_after_seq: 0 }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const address = server.address(); if (!address || typeof address === 'string') throw new Error('本地保存 fixture 未启动');
  const env: Record<string, string> = { ...Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined)),
    AGENTGIT_API_TOKEN: TOKEN, AGENTGIT_API_URL: `http://127.0.0.1:${address.port}`, AGENTGIT_DESKTOP_PROFILE_DIR: join(evidence, 'profile') };
  for (const key of Object.keys(env)) if (['path', 'node_path', 'node_options', 'openai_api_key'].includes(key.toLowerCase())) delete env[key];
  env.PATH = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32');
  const desktop = await electron.launch({ executablePath: executable, args: [], cwd: evidence, env }).catch(async error => {
    server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); throw error;
  });
  try {
    const page = await desktop.firstWindow(), errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message)); page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); });
    await expect(page).toHaveTitle('AgentGit'); expect(page.url()).toBe('agentgit://app/index.html');
    expect(await desktop.evaluate(({ app }) => app.isPackaged)).toBe(true);
    await page.getByLabel('已有项目 ID').fill(REPO); await page.getByRole('button', { name: '选择项目', exact: true }).click();
    await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.getByRole('button', { name: '查看时间线 →', exact: true }).click();
    await page.getByRole('tab', { name: '产物文件', exact: true }).click();
    await expect(page.getByText('native-version.txt', { exact: true })).toBeVisible();
    const packet = { evidence, output, executable, pid: await desktop.evaluate(() => process.pid) };
    await writeFile('../.agentgit/evidence/S6-native-short-request.json', JSON.stringify({ ...packet, phase: 'save' }));
    await page.getByRole('button', { name: '另存为', exact: true }).click();
    await expect(page.getByText('所选版本已保存', { exact: true })).toBeVisible({ timeout: 300000 });
    expect(await readFile(output)).toEqual(bytes);
    await page.screenshot({ path: join(evidence, 'saved.png'), fullPage: true });
    await writeFile('../.agentgit/evidence/S6-native-short-request.json', JSON.stringify({ ...packet, phase: 'cancel' }));
    await page.getByRole('button', { name: '另存为', exact: true }).click();
    await expect(page.getByText('已取消保存', { exact: true })).toBeVisible({ timeout: 300000 });
    expect((await readdir(evidence)).filter(name => name.endsWith('.txt'))).toEqual(['exported-version.txt']);
    expect(await readFile(output)).toEqual(bytes); expect(errors).toEqual([]);
    await writeFile('../.agentgit/evidence/S6-native-short-qa.json', JSON.stringify({ ...packet, object, bytes: bytes.length, saved: true, cancelled: true, errors, fixture: '受控 HTTP 字节；真实主进程和原生对话框，没有 Worker/供应商/容器' }, null, 2));
  } finally {
    try { await desktop.close(); }
    finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
  }
});
