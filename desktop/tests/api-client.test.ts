import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { once } from 'node:events';
import { ApiClient, DesktopError, normalizeBaseUrl } from '../src/main/api-client';

const TOKEN = 'fixture-desktop-token-00000000000000000000';
async function server(handler: (request: IncomingMessage, response: ServerResponse) => void) {
  const value = createServer(handler);
  value.listen(0, '127.0.0.1');
  await once(value, 'listening');
  const address = value.address();
  assert.ok(address && typeof address !== 'string');
  return { url: `http://127.0.0.1:${address.port}`, close: () => new Promise<void>(resolve => {
    value.closeAllConnections(); value.close(() => resolve());
  }) };
}

test('连接地址只允许回环 HTTP 根地址', () => {
  assert.equal(normalizeBaseUrl('http://localhost:8000/'), 'http://127.0.0.1:8000');
  for (const value of ['https://127.0.0.1:8000', 'http://example.com', 'http://0.0.0.0',
    'http://127.0.0.1/private', 'http://user:password@127.0.0.1', 'http://127.0.0.1/?x=1',
    'file:///etc/passwd', 'http://127.0.0.1/#secret']) {
    assert.throws(() => normalizeBaseUrl(value), DesktopError);
  }
});

test('主进程附加身份与项目头，响应和错误不返回令牌', async () => {
  const requests: IncomingMessage[] = [];
  const target = await server((request, response) => {
    requests.push(request);
    response.setHeader('Content-Type', 'application/json');
    if (request.url === '/error') {
      response.writeHead(401); response.end(JSON.stringify({ error: { code: 'unauthorized', message: `bad ${TOKEN}` } }));
    } else response.end(JSON.stringify({ secret: TOKEN, nested: [{ [TOKEN]: TOKEN }] }));
  });
  try {
    const client = new ApiClient(target.url, TOKEN);
    const result = await client.json({ method: 'GET', path: '/runs/example', repository: 'fixture-repo' });
    assert.equal(requests[0]?.headers.authorization, `Bearer ${TOKEN}`);
    assert.equal(requests[0]?.headers['x-repository-id'], 'fixture-repo');
    assert.equal(requests[0]?.headers.origin, undefined);
    assert.ok(!JSON.stringify(result).includes(TOKEN));
    await assert.rejects(client.json({ method: 'GET', path: '/error' }), (error: unknown) => {
      assert.ok(error instanceof DesktopError && error.code === 'unauthorized');
      assert.ok(!error.message.includes(TOKEN)); return true;
    });
    assert.throws(() => client.assertSafeForIpc(Buffer.from(TOKEN)), DesktopError);
  } finally { await target.close(); }
});

test('拒绝重定向且不会把身份发送到第二个端点', async () => {
  let secondRequests = 0;
  const second = await server((_, response) => { secondRequests += 1; response.end('{}'); });
  const first = await server((_, response) => { response.writeHead(302, { Location: second.url }); response.end(); });
  try {
    await assert.rejects(new ApiClient(first.url, TOKEN).json({ method: 'GET', path: '/redirect' }),
      (error: unknown) => error instanceof DesktopError && error.code === 'redirect_forbidden');
    assert.equal(secondRequests, 0);
  } finally { await Promise.all([first.close(), second.close()]); }
});

test('有界响应和超时，未知 POST 不自动重发', async () => {
  let posts = 0;
  const target = await server((request, response) => {
    if (request.method === 'POST') { posts += 1; return; }
    response.end('x'.repeat(1024));
  });
  try {
    const client = new ApiClient(target.url, TOKEN, 80);
    await assert.rejects(client.bytes({ method: 'GET', path: '/large', maxBytes: 100 }),
      (error: unknown) => error instanceof DesktopError && error.code === 'output_limit');
    await assert.rejects(client.json({ method: 'POST', path: '/tasks', body: {}, key: 'same-key' }),
      (error: unknown) => error instanceof DesktopError && error.code === 'outcome_unknown');
    assert.equal(posts, 1);
  } finally { await target.close(); }
});

test('取消页面读取不取消正在提交的动作', async () => {
  let reads = 0, posts = 0;
  const target = await server((request, response) => {
    if (request.method === 'POST') { posts += 1; setTimeout(() => response.end('{}'), 80); }
    else { reads += 1; }
  });
  try {
    const client = new ApiClient(target.url, TOKEN, 1000);
    const reading = client.json({ method: 'GET', path: '/events' });
    const rejected = assert.rejects(reading, (error: unknown) => error instanceof DesktopError && error.code === 'request_cancelled');
    const mutation = client.json({ method: 'POST', path: '/tasks', body: {}, key: 'once' });
    while (!reads || !posts) await new Promise(resolve => setTimeout(resolve, 5));
    client.cancelReads();
    await rejected;
    assert.deepEqual(await mutation, {});
    assert.equal(posts, 1);
  } finally { await target.close(); }
});
