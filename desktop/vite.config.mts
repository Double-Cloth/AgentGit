import { defineConfig } from 'vite';
import { resolve } from 'node:path';

export default defineConfig({
  root: resolve(import.meta.dirname, 'src/renderer'),
  base: './',
  build: { outDir: resolve(import.meta.dirname, '.build/renderer'), emptyOutDir: true, sourcemap: false },
  server: { host: '127.0.0.1', port: 5173, strictPort: true },
});
