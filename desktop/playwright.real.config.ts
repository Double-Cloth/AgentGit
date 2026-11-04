import { defineConfig } from '@playwright/test';

const evidence = process.env.AGENTGIT_ELECTRON_EVIDENCE_PREFIX;
if (evidence && !/^[a-zA-Z0-9_-]{1,80}$/.test(evidence)) throw new Error('测试证据前缀无效');

export default defineConfig({ testDir: './tests/electron', testMatch: '**/real-backend.spec.ts', outputDir: evidence ? `./test-results/${evidence}-real` : './test-results/real', workers: 1,
  timeout: 240000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: evidence ? `../.agentgit/evidence/${evidence}-real.xml` : '../.agentgit/evidence/S6-electron-real.xml' }]],
  use: { trace: 'retain-on-failure' } });
