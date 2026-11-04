// 在构建前核对项目声明的工具链，避免 PATH 漂移到不兼容的 Node。
const { engines } = require('../package.json');
const expected = `v${engines.node}`;
if (process.version !== expected) {
  throw new Error(`构建需要 Node ${expected}，当前为 ${process.version}。请切换固定运行时后重试。`);
}
const npm = process.env.npm_config_user_agent?.match(/^npm\/([^ ]+)/)?.[1];
if (npm !== engines.npm) {
  throw new Error(`构建需要 npm ${engines.npm}，当前为 ${npm ?? '未通过 npm 启动'}。`);
}
console.log(`构建工具链已核对：Node ${process.version} / npm ${npm}`);
