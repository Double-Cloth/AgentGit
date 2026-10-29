import { defineConfig } from '@playwright/test';
import { EVIDENCE } from './tests/support/desktop-runtime';

export default defineConfig({ testDir: './tests/electron', testMatch: '**/mcp*.spec.ts', outputDir: `./test-results/${EVIDENCE}-mcp`, workers: 1,
  timeout: 180000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: `../.agentgit/evidence/${EVIDENCE}-mcp.xml` }]], use: { trace: 'retain-on-failure' } });
