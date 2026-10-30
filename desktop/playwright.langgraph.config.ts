import { defineConfig } from '@playwright/test';
import { EVIDENCE } from './tests/support/desktop-runtime';

export default defineConfig({ testDir: './tests/electron', testMatch: '**/langgraph-real.spec.ts', outputDir: `./test-results/${EVIDENCE}-langgraph`, workers: 1,
  timeout: 180000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: `../.agentgit/evidence/${EVIDENCE}-langgraph.xml` }]], use: { trace: 'retain-on-failure' } });
