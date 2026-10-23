import { defineConfig } from '@playwright/test';

export default defineConfig({ testDir: './tests/electron', testMatch: '**/real-backend.spec.ts', workers: 1,
  timeout: 240000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: '../.agentgit/evidence/S6-electron-real.xml' }]],
  use: { trace: 'retain-on-failure' } });
