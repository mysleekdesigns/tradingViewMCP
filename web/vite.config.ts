import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The local backend (server/) on 127.0.0.1:8787; the browser uses same-origin URLs.
// Shared by `vite` (dev) and `vite preview` (built app).
const backendProxy = {
  '/api': { target: 'http://127.0.0.1:8787', changeOrigin: false },
  '/ws': { target: 'http://127.0.0.1:8787', ws: true, changeOrigin: false },
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: { proxy: backendProxy },
  preview: { proxy: backendProxy },
  test: {
    // Pure mappers and indicators run in Node; DOM/chart tests opt in per file
    // with `// @vitest-environment jsdom`.
    environment: 'node',
  },
})
