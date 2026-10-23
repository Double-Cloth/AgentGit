import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { APP_DOCUMENT, assetPath, assertSender, trustedDocument } from '../src/main/security';
import { DesktopError } from '../src/main/api-client';

test('IPC 必须同时匹配窗口、顶层 frame 和受信任文档', () => {
  assertSender({ webContentsId: 10, mainFrame: true, url: APP_DOCUMENT + '#timeline' }, 10);
  for (const sender of [
    { webContentsId: 11, mainFrame: true, url: APP_DOCUMENT },
    { webContentsId: 10, mainFrame: false, url: APP_DOCUMENT },
    ...['https://app/index.html', 'agentgit://app/other.html', APP_DOCUMENT + '?x=1',
      'agentgit://user@app/index.html', 'data:text/html,test', 'about:blank'].map(url =>
      ({ webContentsId: 10, mainFrame: true, url })),
  ]) assert.throws(() => assertSender(sender, 10), DesktopError);
  assert.equal(trustedDocument('not a URL'), false);
});

test('自定义协议只提供包内页面与固定类型的静态资源', () => {
  const root = resolve('fixture-renderer');
  assert.equal(assetPath(root, APP_DOCUMENT), resolve(root, 'index.html'));
  assert.equal(assetPath(root, 'agentgit://app/assets/main-a.js'), resolve(root, 'assets/main-a.js'));
  for (const url of ['https://app/assets/main.js', 'agentgit://other/assets/main.js',
    'agentgit://app/secret.json', 'agentgit://app/assets/private.json',
    'agentgit://app/assets/%2e%2e%2fsecret.js', 'agentgit://app/assets/a%5cb.js',
    'agentgit://app/assets/%00.js', 'agentgit://app/assets/x.js?url=https://evil.example',
    'agentgit://app/assets/x.js#fragment', 'agentgit://app/assets/C:/secret.js',
    'agentgit://app/assets/%zz.js', 'file:///index.html']) {
    assert.throws(() => assetPath(root, url), DesktopError);
  }
});
