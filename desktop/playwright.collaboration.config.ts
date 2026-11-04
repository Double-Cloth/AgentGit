import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const root = process.env.AGENTGIT_COLLABORATION_DESKTOP_QA ?? join(tmpdir(), 'agentgit-collaboration-desktop');
export default defineConfig({ testDir: './tests/electron', testMatch: '**/collaboration-real.spec.ts', outputDir: join(root, 'results'), workers: 1,
  timeout: 180000, expect: { timeout: 15000 }, reporter: [['list'], ['junit', { outputFile: join(root, 'junit.xml') }]], use: { trace: 'retain-on-failure' } });
