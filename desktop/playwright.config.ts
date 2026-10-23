import { defineConfig } from '@playwright/test';

export default defineConfig({ testDir: './tests/electron', testMatch: '**/boundary.spec.ts', workers: 1, fullyParallel: false,
  timeout: 45000, expect: { timeout: 10000 }, reporter: [['list'], ['junit', { outputFile: '../.agentgit/evidence/S6-electron-boundary.xml' }]],
  use: { trace: 'retain-on-failure' } });
