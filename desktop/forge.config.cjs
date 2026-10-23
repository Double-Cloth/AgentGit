module.exports = {
  packagerConfig: {
    asar: true,
    executableName: 'AgentGit',
    appBundleId: 'local.agentgit.desktop',
    // 只分发运行所需内容，避免新增验收配置或失败轨迹进入程序包。
    ignore: [/^\/(?!\.build(?:\/|$)|node_modules(?:\/|$)|package\.json$)/, /^\/node_modules\/\.cache(?:\/|$)/],
  },
  makers: [
    { name: '@electron-forge/maker-squirrel', config: { name: 'AgentGit', setupExe: 'AgentGitSetup.exe' } },
    { name: '@electron-forge/maker-zip', platforms: ['win32'] },
  ],
};
