import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));
export default defineConfig(({ command, mode }) => {
  const surface = mode === 'app' ? 'app' : 'admin';
  // Keep the repository root in dev so both /admin/ and /app/ are addressable,
  // while preserving the existing flat production artifact layout.
  const root = command === 'serve' ? here : resolve(here, surface);
  return {
    root,
    base: command === 'serve' ? '/' : `/${surface}/`,
    plugins: [react()],
    build: {
      outDir: resolve(here, 'dist', surface),
      emptyOutDir: true,
      target: 'es2022',
    },
    server: { fs: { allow: [here] }, proxy: { '/api': 'http://127.0.0.1:8080', '/sub': 'http://127.0.0.1:8080', '/open': 'http://127.0.0.1:8080' } },
  };
});
