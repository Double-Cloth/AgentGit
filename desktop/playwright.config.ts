import { defineConfig } from '@playwright/test';

const evidence = process.env.AGENTGIT_ELECTRON_EVIDENCE_PREFIX;
if (evidence && !/^[a-zA-Z0-9_-]{1,80}$/.test(evidence)) throw new Error('测试证据前缀无效');

export default defineConfig({ testDir: './tests/electron', testMatch: '**/boundary.spec.ts', outputDir: evidence ? `./test-results/${evidence}-boundary` : './test-results/boundary', workers: 1, fullyParallel: false,
  timeout: 45000, expect: { timeout: 10000 }, reporter: [['list'], ['junit', { outputFile: evidence ? `../.agentgit/evidence/${evidence}-boundary.xml` : '../.agentgit/evidence/S6-electron-boundary.xml' }]],
  use: { trace: 'retain-on-failure' } });
