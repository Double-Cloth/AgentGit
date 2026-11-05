// 在构建前核对项目声明的工具链，避免 PATH 漂移到不兼容的 Node。
// engines.node 支持精确版本（如 24.19.0）或以 x 结尾的区间（如 26.x），
// 区间仅放过声明的版本位，防止漂移到其他主版本。
const { engines } = require('../package.json');

function nodeMatches (actual, declared) {
  const actualParts = actual.split('.');
  const declaredParts = declared.split('.');
  return declaredParts.every((part, i) => part === 'x' || part === '*' || part === actualParts[i]);
}

const actual = process.version.replace(/^v/, '');
if (!nodeMatches(actual, engines.node)) {
  throw new Error(`构建需要 Node ${engines.node}，当前为 ${process.version}。请切换固定运行时后重试。`);
}
const npm = process.env.npm_config_user_agent?.match(/^npm\/([^ ]+)/)?.[1];
if (npm !== engines.npm) {
  throw new Error(`构建需要 npm ${engines.npm}，当前为 ${npm ?? '未通过 npm 启动'}。`);
}
console.log(`构建工具链已核对：Node ${process.version} / npm ${npm}`);
