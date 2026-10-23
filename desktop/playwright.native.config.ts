import { defineConfig } from '@playwright/test';

export default defineConfig({ testDir: './tests/electron', testMatch: '**/native-save.spec.ts', workers: 1,
  timeout: 720000, expect: { timeout: 10000 }, reporter: [['list'], ['junit', { outputFile: '../.agentgit/evidence/S6-native-short.xml' }]] });
