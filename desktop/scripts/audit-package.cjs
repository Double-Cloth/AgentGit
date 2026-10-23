// 核验 Forge 产物与本地构建一致；不启动应用，也不修改包内容。
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const asar = require('@electron/asar');

const desktop = path.resolve(__dirname, '..');
const packaged = path.resolve(process.argv[2] || path.join(desktop, 'out/AgentGit-win32-x64'));
const archive = path.join(packaged, 'resources/app.asar');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const entries = asar.listPackage(archive).map(name => name.replaceAll('\\', '/'));
const forbidden = entries.filter(name => /^\/(?:src|tests|scripts|\.agentgit|\.env(?:\.[^/]*)?|playwright.*\.config\.ts|tsconfig.*\.json)(?:\/|$)/.test(name));
assert.deepEqual(forbidden, [], '包内包含开发源码、测试或配置');

function files(root) {
  return fs.readdirSync(root, { withFileTypes: true }).flatMap(entry => {
    const full = path.join(root, entry.name);
    return entry.isDirectory() ? files(full) : entry.isFile() ? [full] : [];
  });
}
const build = files(path.join(desktop, '.build')).map(file => {
  const relative = path.relative(desktop, file).replaceAll('\\', '/');
  const expected = fs.readFileSync(file), actual = asar.extractFile(archive, path.normalize(relative));
  assert.ok(expected.equals(actual), `包内构建文件不一致：${relative}`);
  return { file: relative, bytes: actual.length, sha256: hash(actual) };
});
const fontManifest = JSON.parse(fs.readFileSync(path.join(desktop, 'src/renderer/public/licenses/manifest.json'), 'utf8'));
for (const file of fontManifest.files) {
  const location = file.file.endsWith('.ttf')
    ? build.filter(item => item.file.startsWith(`.build/renderer/assets/${path.parse(file.file).name}-`) && item.file.endsWith('.ttf'))
    : build.filter(item => item.file === `.build/renderer/licenses/${file.file}`);
  assert.equal(location.length, 1, `缺少字体/授权：${file.file}`);
  assert.equal(location[0].sha256, file.sha256, `上游字体/授权字节不一致：${file.file}`);
}
const manifest = JSON.parse(asar.extractFile(archive, 'package.json').toString('utf8'));
assert.equal(manifest.main, '.build/main/index.js');
assert.ok(!manifest.devDependencies && !manifest.scripts, '包内不应保留构建脚本或开发依赖');
const dependencies = entries.filter(name => /^\/node_modules\/(?:@[^/]+\/)?[^/]+\/package\.json$/.test(name)).map(name => {
  const value = JSON.parse(asar.extractFile(archive, path.normalize(name.slice(1))).toString('utf8'));
  return { name: value.name, version: value.version, license: value.license ?? null };
});
const knownToken = process.env.AGENTGIT_API_TOKEN;
if (knownToken) for (const file of build) {
  assert.ok(!asar.extractFile(archive, path.normalize(file.file)).includes(Buffer.from(knownToken)), '构建文件包含当前 API 凭据');
}
const result = { schema_version: 1, product: manifest.name, version: manifest.version, platform: 'win32', arch: 'x64',
  archive: { bytes: fs.statSync(archive).size, sha256: hash(fs.readFileSync(archive)), entries: entries.length },
  forbidden, build, dependencies, fonts: { upstream_commit: fontManifest.commit, verified: true },
  credential_scan: knownToken ? '当前进程凭据未命中' : '无当前凭据，仅执行包内容白名单与字节一致性检查' };
if (process.argv[3]) fs.writeFileSync(path.resolve(process.argv[3]), JSON.stringify(result, null, 2));
console.log(JSON.stringify({ product: result.product, version: result.version, entries: entries.length,
  build_files: build.length, fonts_verified: true, forbidden, dependencies }));
