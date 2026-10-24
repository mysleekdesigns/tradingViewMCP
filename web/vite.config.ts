import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    // Pure mappers and indicators run in Node; DOM/chart tests opt in per file
    // with `// @vitest-environment jsdom`.
    environment: 'node',
  },
})
