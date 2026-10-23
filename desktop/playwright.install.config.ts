import { defineConfig } from '@playwright/test';

export default defineConfig({ testDir: './tests/electron', testMatch: '**/install.spec.ts', outputDir: './test-results/install', workers: 1,
  timeout: 300000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: '../.agentgit/evidence/S6-electron-install.xml' }]],
  use: { trace: 'retain-on-failure' } });
