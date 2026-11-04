import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PreferenceStore } from '../src/main/preferences';

test('窗口重开保留同一端点的项目，切换端点清除旧项目；偏好不保留其他字段', async () => {
  const root = await mkdtemp(join(tmpdir(), 'agentgit-preferences-'));
  const repositoryId = '00000000-0000-4000-8000-000000000001';
  await writeFile(join(root, 'preferences.json'), JSON.stringify({ baseUrl: 'http://127.0.0.1:8000', repositoryId,
    token: 'must-not-persist', bounds: { width: 1300, height: 800 } }));
  const store = new PreferenceStore(root);
  assert.equal((await store.load('http://localhost:8000')).repositoryId, repositoryId);
  await Promise.all([store.update({ bounds: { width: 900, height: 700 } }), store.update({ bounds: { width: 1000, height: 750 } })]);
  const content = await readFile(join(root, 'preferences.json'), 'utf8');
  assert.ok(!content.includes('token')); assert.ok(!content.includes('must-not-persist'));
  const reopened = await new PreferenceStore(root).load();
  assert.deepEqual(reopened.bounds, { width: 1000, height: 750 }); assert.equal(reopened.repositoryId, repositoryId);
  assert.equal((await store.load('http://localhost:8001')).repositoryId, null);
});
