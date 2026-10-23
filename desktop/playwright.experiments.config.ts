import { defineConfig } from '@playwright/test';

export default defineConfig({ testDir: './tests/electron', testMatch: '**/experiments*.spec.ts', outputDir: './test-results/experiments', workers: 1,
  timeout: 600000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: '../.agentgit/evidence/S7-desktop-real.xml' }]],
  use: { trace: 'retain-on-failure' } });
