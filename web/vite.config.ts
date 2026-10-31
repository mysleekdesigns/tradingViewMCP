import babel from '@rolldown/plugin-babel'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The local backend (server/) on 127.0.0.1:8787 and the lab on 127.0.0.1:8788; the browser
// uses same-origin URLs.
// Shared by `vite` (dev) and `vite preview` (built app).
const backendProxy = {
  '/api': { target: 'http://127.0.0.1:8787', changeOrigin: false },
  '/ws': { target: 'http://127.0.0.1:8787', ws: true, changeOrigin: false },
  // The Python lab service (lab/, FastAPI): backtests, feature registry, trial ledger.
  '/lab': { target: 'http://127.0.0.1:8788', changeOrigin: false },
}

/**
 * Vendor chunks (Phase 19, D2): each library gets its own file, so an app-only change keeps
 * their hashes and the browser cache. `scheduler` ships with react-dom; `fancy-canvas` is
 * Lightweight Charts' canvas helper. Rolldown's `codeSplitting.groups` replaces `manualChunks`.
 */
export const VENDOR_CHUNKS = [
  { name: 'react', test: /[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 30 },
  {
    name: 'lightweight-charts',
    test: /[\\/]node_modules[\\/](lightweight-charts|fancy-canvas)[\\/]/,
    priority: 20,
  },
  // Below Lightweight Charts: a group also takes its modules' dependencies, so at a higher
  // priority this one would swallow the chart library it imports.
  {
    name: 'lwc-accessibility',
    test: /[\\/]node_modules[\\/]@tradingview[\\/]lwc-plugin-accessibility[\\/]/,
    priority: 10,
  },
]

/**
 * React Compiler (Phase 19, D4), through Babel (`@vitejs/plugin-react` v6 has no Babel of its
 * own). 'annotation' (owner decision, 2026-10-30): only functions marked "use memo" are compiled,
 * i.e. the 13 shell components (Header, Watchlist and its rows, StatusBar, SidebarRail,
 * DrawingToolbar, AlertsPanel, QuoteView/QuoteReadout, LastUpdateText/LastUpdateClock, App's
 * LiveHeader/LiveStatusBar). 'infer' (every component and hook) costs ~24 KB gzip on the main
 * chunk; 'all' is never used: it also rewrites plain helpers such as the indicator steppers with
 * a `useMemoCache` hook, which throws when they run outside a render.
 */
export const REACT_COMPILER_MODE = 'annotation'

/**
 * `REACT_COMPILER_DIAGNOSTICS=1 npx vite build` prints every function the compiler skipped or
 * bailed out of (a bailout leaves that function exactly as written, so it is never an error).
 */
const compilerLogger =
  process.env.REACT_COMPILER_DIAGNOSTICS === '1'
    ? {
        logEvent(filename: string | null, event: { kind: string; fnLoc?: unknown }) {
          if (event.kind === 'CompileSuccess' || event.kind === 'Timing') return
          const line = (event.fnLoc as { start?: { line?: number } } | null)?.start?.line
          console.warn(`[react-compiler] ${event.kind} ${filename ?? '?'}:${line ?? '?'}`, event)
        },
      }
    : null

/**
 * Modules the compiler leaves exactly as written. `chart/Chart.tsx`: its `Chart` component is
 * skipped by the compiler anyway (try/finally, a counter updated inside a callback), and
 * `LiveIndicatorRows` re-renders from an indicator-store subscription whose snapshot it does not
 * use, then reads `manager.legendPartition()`; compiled, that read would be cached on the props
 * and the legend would stop following live ticks.
 */
export const REACT_COMPILER_EXCLUDE: RegExp[] = [/[\\/]src[\\/]chart[\\/]Chart\.tsx(?:\?|$)/]

const compilerPreset = reactCompilerPreset({
  compilationMode: REACT_COMPILER_MODE,
  ...(compilerLogger === null ? {} : { logger: compilerLogger }),
})

export const reactCompiler = {
  ...compilerPreset,
  rolldown: {
    ...compilerPreset.rolldown,
    filter: { ...compilerPreset.rolldown.filter, id: { exclude: REACT_COMPILER_EXCLUDE } },
  },
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), babel({ presets: [reactCompiler] })],
  server: {
    proxy: backendProxy,
    // The two largest modules: transform them while the dev server starts, not on first request.
    warmup: { clientFiles: ['./src/App.tsx', './src/chart/Chart.tsx'] },
  },
  build: {
    rolldownOptions: { output: { codeSplitting: { groups: VENDOR_CHUNKS } } },
  },
  preview: { proxy: backendProxy },
  test: {
    // Pure mappers and indicators run in Node; DOM/chart tests opt in per file
    // with `// @vitest-environment jsdom`.
    environment: 'node',
  },
})
