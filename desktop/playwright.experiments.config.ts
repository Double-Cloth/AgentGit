import { defineConfig } from '@playwright/test';
import { EVIDENCE } from './tests/support/desktop-runtime';

export default defineConfig({ testDir: './tests/electron', testMatch: '**/experiments*.spec.ts', outputDir: `./test-results/${EVIDENCE}-experiments`, workers: 1,
  timeout: 600000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: `../.agentgit/evidence/${EVIDENCE}-experiments.xml` }]],
  use: { trace: 'retain-on-failure' } });
