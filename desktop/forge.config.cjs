module.exports = {
  packagerConfig: {
    asar: true,
    executableName: 'AgentGit',
    appBundleId: 'local.agentgit.desktop',
    ignore: [
      /^\/(?:src|tests|scripts|\.test-build|out|node_modules\/\.cache)(?:\/|$)/,
      /^\/(?:tsconfig.*\.json|vite\.config\.mts|playwright(?:\.real)?\.config\.ts|\.node-version)$/,
    ],
  },
  makers: [
    { name: '@electron-forge/maker-squirrel', config: { name: 'AgentGit', setupExe: 'AgentGitSetup.exe' } },
    { name: '@electron-forge/maker-zip', platforms: ['win32'] },
  ],
};
