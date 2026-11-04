import { defineConfig } from 'vite';
import { resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const metadata = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version: string };

export default defineConfig({
  root: resolve(import.meta.dirname, 'src/renderer'),
  base: './',
  define: { __AGENTGIT_DESKTOP_VERSION__: JSON.stringify(metadata.version) },
  build: { outDir: resolve(import.meta.dirname, '.build/renderer'), emptyOutDir: true, sourcemap: false },
  server: { host: '127.0.0.1', port: 5173, strictPort: true },
});
