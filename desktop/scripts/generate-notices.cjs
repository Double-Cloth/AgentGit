// 按已安装的锁定依赖生成授权原文和清单，不联网、不执行依赖代码。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { createHash } = require('node:crypto');
const root = path.resolve(__dirname, '..');
const visited = new Map();
function visit(name, from) {
  const search = createRequire(path.join(from, 'package.json')).resolve.paths(name) || [];
  const metadata = search.map(folder => path.join(folder, name, 'package.json')).find(file => fs.existsSync(file));
  assert.ok(metadata, `依赖尚未安装：${name}`);
  const value = JSON.parse(fs.readFileSync(metadata, 'utf8'));
  const identity = `${value.name}@${value.version}`;
  if (visited.has(identity)) return;
  const directory = path.dirname(metadata);
  const licenses = fs.readdirSync(directory).filter(file => /^(?:licen[sc]e(?:[.-]|$)|copying(?:\.|$)|notice(?:\.|$))/i.test(file)
    && fs.statSync(path.join(directory, file)).isFile()).sort().map(file => {
    const bytes = fs.readFileSync(path.join(directory, file));
    return { file, sha256: createHash('sha256').update(bytes).digest('hex'), text: bytes.toString('utf8') };
  });
  assert.ok(licenses.length, `依赖缺少授权原文：${identity}`);
  visited.set(identity, { name: value.name, version: value.version, license: value.license || null, licenses });
  for (const child of Object.keys(value.dependencies || {}).sort()) visit(child, directory);
}
for (const name of ['react', 'react-dom', '@xyflow/react', 'electron-squirrel-startup']) visit(name, root);
const packages = [...visited.values()].sort((a, b) => `${a.name}@${a.version}`.localeCompare(`${b.name}@${b.version}`, 'en'));
const heading = 'AgentGit 第三方软件授权说明\n\n本文件记录已锁定前端及主进程运行依赖的声明依赖闭包（含类型声明包）。Electron/Chromium 的授权另随程序目录中的 LICENSE 与 LICENSES.chromium.html 提供。字体原文见包内 licenses/；下列授权原文保留第三方原文。\n';
const text = heading + packages.map(item => `\n${'='.repeat(72)}\n${item.name} ${item.version} · ${item.license || '详见原文'}\n`
  + item.licenses.map(license => `\n--- ${license.file} ---\n${license.text}`).join('\n')).join('\n');
const publicFolder = path.join(root, 'src/renderer/public/licenses');
fs.mkdirSync(publicFolder, { recursive: true });
fs.writeFileSync(path.join(root, 'src/renderer/assets/THIRD_PARTY_NOTICES.txt'), text);
fs.writeFileSync(path.join(publicFolder, 'THIRD_PARTY_NOTICES.txt'), text);
fs.writeFileSync(path.join(publicFolder, 'npm-dependencies.json'), JSON.stringify({ schema_version: 1,
  packages: packages.map(({ licenses, ...item }) => ({ ...item, licenses: licenses.map(({ text, ...entry }) => entry) })) }, null, 2) + '\n');
console.log(`已生成 ${packages.length} 个声明依赖的授权说明`);
