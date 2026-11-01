import { defineConfig } from '@playwright/test';

const evidence = process.env.AGENTGIT_ELECTRON_EVIDENCE_PREFIX;
if (!evidence || !/^[a-zA-Z0-9_-]{1,80}$/.test(evidence)) throw new Error('必须提供唯一测试证据前缀');
export default defineConfig({ testDir: './tests/electron', testMatch: '**/a2a-real.spec.ts', outputDir: `./test-results/${evidence}-a2a`, workers: 1, fullyParallel: false,
  timeout: 240000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: `../.agentgit/evidence/${evidence}/electron.xml` }]], use: { trace: 'retain-on-failure' } });
