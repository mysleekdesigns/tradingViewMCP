import { describe, expect, it } from 'vitest'
import indexHtml from '../index.html?raw'
import viteConfig, { VENDOR_CHUNKS } from '../vite.config'
import appSource from './App.tsx?raw'
import chartSource from './chart/Chart.tsx?raw'
import headerSource from './components/Header.tsx?raw'
import lazySource from './components/lazyPanels.tsx?raw'
import railSource from './components/SidebarRail.tsx?raw'
import mainSource from './main.tsx?raw'

// Vitest turns CSS imports (even `?raw`) into empty strings, so stylesheets are read from disk.
// The app tsconfig has no Node types, hence the narrow local type for `node:fs`.
const fs = (
  globalThis as unknown as {
    process: {
      getBuiltinModule(id: 'node:fs'): {
        readFileSync(p: URL, enc: 'utf8'): string
        readdirSync(p: URL, o: { recursive: true }): string[]
      }
    }
  }
).process.getBuiltinModule('node:fs')
const read = (path: string) => fs.readFileSync(new URL(path, import.meta.url), 'utf8')
/** Every non-test source module under src/, as [path relative to src, source]. */
const SOURCES: [string, string][] = fs
  .readdirSync(new URL('./', import.meta.url), { recursive: true })
  .filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
  .map((f) => [f.replace(/\\/g, '/'), read(`./${f}`)])
const indexCss = read('./index.css')
const researchCss = read('./components/Research.css')
const indicatorsCss = read('./components/Indicators.css')
const backtestCss = read('./components/Backtest.css')
const backtestShellCss = read('./components/BacktestShell.css')
const sans400Css = read('../node_modules/@fontsource/ibm-plex-sans/latin-400.css')
const mono400Css = read('../node_modules/@fontsource/ibm-plex-mono/latin-400.css')

// Phase 19 wave 4 (D1-D3): what keeps the five heavy views out of the main chunk, the vendor
// libraries in their own stable chunks, and the fonts preloaded without a second download.

const LAZY_MODULES = [
  'BacktestPanel',
  'ResearchPage',
  'PaperTradingPanel',
  'SeasonalsView',
  'SettingsDialog',
] as const

/** Every `import … from '<spec>'` statement (multi-line too) that is not `import type`. */
function valueImports(source: string, spec: string): string[] {
  const re = /^import\s+([\s\S]*?)\s+from\s+'([^']+)'/gm
  const out: string[] = []
  for (const m of source.matchAll(re)) {
    if (m[2] === spec && !m[1].startsWith('type ')) out.push(m[0])
  }
  return out
}

describe('lazy views stay out of the main chunk (D1)', () => {
  it.each(LAZY_MODULES)('App imports nothing but types from %s', (mod) => {
    expect(valueImports(appSource, `./components/${mod}`)).toEqual([])
  })

  it.each(LAZY_MODULES)('the rail and lazyPanels import %s only dynamically', (mod) => {
    expect(valueImports(railSource, `./${mod}`)).toEqual([])
    expect(valueImports(lazySource, `./${mod}`)).toEqual([])
    expect(lazySource).toContain(`import('./${mod}')`)
  })

  it('App renders the lazy wrappers, each inside its own Suspense boundary', () => {
    expect(appSource).toMatch(/from '\.\/components\/lazyPanels'/)
    const suspenses = appSource.match(/<Suspense\b/g) ?? []
    // Paper, dock, Research, Settings, Seasonals. (Research's render-error panel needed a sixth
    // while it was lazy; it is a static import now.)
    expect(suspenses).toHaveLength(5)
    // ...and each with a LazyBoundary, so a chunk that fails to load stays in its slot.
    const boundaries = [...appSource.matchAll(/<LazyBoundary\s+variant="(\w+)"/g)].map((m) => m[1])
    expect(boundaries.sort()).toEqual(['dialog', 'dialog', 'dock', 'overlay', 'sidebar'])
    for (const label of [
      'Loading paper trading…',
      'Loading Strategy Tester…',
      'Loading Research…',
      'Loading Settings…',
      'Loading Seasonals…',
    ]) {
      expect(appSource).toContain(`label="${label}"`)
    }
  })

  it('the shared icons come from the small icons module', () => {
    expect(valueImports(railSource, './icons')).toHaveLength(1)
  })
})

// D4 follow-up: three rarely opened dialogs load on first open too (SymbolSearch stays eager: it
// opens on typing). Each lazyPanels wrapper carries its own Suspense boundary.
const LAZY_DIALOGS = ['IndicatorSettings', 'AlertDialog', 'ShortcutsDialog'] as const

describe('lazy dialogs stay out of the main chunk', () => {
  it.each(LAZY_DIALOGS)('App imports nothing but types from %s', (mod) => {
    expect(valueImports(appSource, `./components/${mod}`)).toEqual([])
  })

  it.each(LAZY_DIALOGS)('lazyPanels imports %s only dynamically and wraps it', (mod) => {
    expect(valueImports(lazySource, `./${mod}`)).toEqual([])
    expect(lazySource).toContain(`import('./${mod}')`)
    expect(lazySource).toContain(`export function ${mod}(`)
    expect(appSource).toContain(`<${mod}`)
  })

  it('no other shell module imports them statically', () => {
    for (const src of [railSource, headerSource, chartSource, mainSource]) {
      for (const mod of LAZY_DIALOGS) expect(valueImports(src, `./${mod}`)).toEqual([])
    }
  })

  it('each lazy dialog shows the shared dialog placeholder while it loads', () => {
    for (const label of ['Loading indicator settings…', 'Loading alert…', 'Loading shortcuts…']) {
      expect(lazySource).toContain(`label="${label}"`)
    }
    expect(lazySource.match(/<PanelLoading\s+variant="dialog"/g)).toHaveLength(3)
    // A failed download shows the dialog-sized error card in the same place.
    expect(lazySource.match(/<LazyBoundary\s+variant="dialog"/g)).toHaveLength(3)
  })

  it('SymbolSearch stays in the main chunk (it opens on a keystroke)', () => {
    expect(valueImports(appSource, './components/SymbolSearch')).toHaveLength(1)
  })

  it('the stylesheets the main chunk still needs stay imported by the shell', () => {
    // Indicators.css (the dialog backdrop) and Alerts.css stay with the header and alerts panel;
    // only Shortcuts.css, used by the shortcuts dialog alone, moves to its chunk.
    expect(headerSource).toContain("import './Indicators.css'")
    expect(read('./components/AlertsPanel.tsx')).toContain("import './Alerts.css'")
    expect(read('./components/AlertToasts.tsx')).toContain("import './Alerts.css'")
    expect(read('./components/ShortcutsDialog.tsx')).toContain("import './Shortcuts.css'")
  })
})

describe('fallback and shell styles live in the main stylesheet', () => {
  it('the placeholder classes are in index.css', () => {
    for (const cls of [
      '.lazy-panel {',
      '.app > .lazy-panel--overlay {',
      '.lazy-panel--card {',
      '.lazy-panel__status {',
      '.lazy-panel__spinner {',
    ]) {
      expect(indexCss).toContain(cls)
    }
    // The dock placeholder reuses `.tester`, whose frame is in BacktestShell.css (with the chart);
    // the rest of Backtest.css ships with the panel (see "Backtest CSS splits" below).
    expect(chartSource).toContain("import '../components/BacktestShell.css'")
    expect(backtestShellCss).toContain('\n.tester {')
    // The dialog placeholder reuses the shared backdrop.
    expect(indexCss).toContain('.modal-backdrop {')
    expect(indicatorsCss).toContain('.modal-backdrop--center {')
    expect(headerSource).toContain("import './Indicators.css'")
  })

  it('the "Restore previous plan" notice is styled before Research has ever loaded', () => {
    // App can show it at startup (a saved restore offer), with Research.css not yet loaded.
    for (const rule of [
      '.app-notice--restore {',
      '.app-notice + .app-notice--restore {',
      '.app-notice__action {',
    ]) {
      expect(indexCss).toContain(rule)
      expect(researchCss).not.toContain(rule)
    }
    // After the base rule, so its border colour still wins at equal specificity.
    expect(indexCss.indexOf('.app-notice--restore {')).toBeGreaterThan(
      indexCss.indexOf('.app-notice {'),
    )
  })
})

describe("Research's render-error panel is in the main chunk", () => {
  const fallbackSource = read('./components/ResearchFallback.tsx')

  it('App imports it statically from its own small module, not from ResearchPage', () => {
    expect(valueImports(appSource, './components/ResearchFallback')).toHaveLength(1)
    expect(lazySource).not.toContain('ResearchFallback')
  })

  it('that module pulls in neither ResearchPage nor its stylesheet', () => {
    const imports = fallbackSource.match(/^import\b[^\n]*$/gm) ?? []
    expect(imports.length).toBeGreaterThan(0)
    for (const line of imports) {
      expect(line).not.toMatch(/ResearchPage|\.css'/)
    }
    expect(fallbackSource).not.toMatch(/import\(/)
    // ResearchPage only re-exports it (the binding lives in the main chunk).
    expect(read('./components/ResearchPage.tsx')).toContain(
      "export { ResearchFallback, type ResearchFallbackProps } from './ResearchFallback'",
    )
  })
})

/** Class names a stylesheet mentions (`.tester__x`), without the dot. */
function classesOf(css: string): Set<string> {
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, '')
  return new Set([...noComments.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((m) => m[1]))
}

/** Rule selectors at the top level of a stylesheet or inside @media blocks. */
function selectorsOf(css: string): string[] {
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, '')
  return [...noComments.matchAll(/(^|[{}])\s*([^{}@]+?)\s*\{/g)]
    .map((m) => m[2].trim())
    .filter((sel) => !/^(from|to|\d+%)$/.test(sel))
}

describe('Backtest CSS splits with the lazily loaded panel', () => {
  // Was a documented exception (Chart.tsx imported all of Backtest.css for the dock frame and the
  // pane legend, keeping 24 KB in the main stylesheet). Those few rules are BacktestShell.css now.

  it('only BacktestPanel.tsx imports Backtest.css, and BacktestPanel itself loads lazily', () => {
    const importers = SOURCES.filter(([, src]) =>
      /import '[./]*(components\/)?Backtest\.css'/.test(src),
    )
    expect(importers.map(([f]) => f)).toEqual(['components/BacktestPanel.tsx'])
    expect(read('./components/BacktestPanel.tsx')).toContain("import './Backtest.css'")
    for (const [f, src] of SOURCES) {
      if (f === 'components/BacktestPanel.tsx') continue
      expect(valueImports(src, './BacktestPanel'), f).toEqual([])
      expect(valueImports(src, './components/BacktestPanel'), f).toEqual([])
    }
  })

  it("Chart.tsx imports the shell part in Backtest.css's old place", () => {
    expect(chartSource).not.toContain('Backtest.css')
    expect(valueImports(chartSource, '../components/BacktestShell.css')).toEqual([])
    expect(chartSource).toContain("import '../components/BacktestShell.css'")
  })

  it('the shell part is exactly the grid row, the dock frame and the pane legend', () => {
    expect(selectorsOf(backtestShellCss)).toEqual([
      '.app.app--with-tester',
      '.app.app--with-tester.app--with-sidebar',
      '.app > .tester',
      '.app--with-tester.app--with-sidebar > .tester',
      '.tester',
      '.bt-pane-legend',
      '.bt-pane-legend__title',
      '.bt-pane-legend__item',
      '.bt-pane-legend__swatch',
      '.bt-pane-legend__value',
      '.bt-pane-legend--chip',
      '.bt-pane-legend__asof',
    ])
    // Not duplicated: none of those rules is left in Backtest.css.
    for (const sel of selectorsOf(backtestShellCss)) {
      expect(selectorsOf(backtestCss)).not.toContain(sel)
    }
  })

  it('what stays in Backtest.css only styles the panel, so loading it later changes no cascade', () => {
    // Every rule is scoped to the dock's own classes (or the body class the panel sets while
    // resizing), so it cannot tie with a rule of another stylesheet on a shell element.
    for (const sel of selectorsOf(backtestCss)) {
      for (const part of sel.split(',')) {
        expect(part.trim(), sel).toMatch(
          /^(\.tester(__[\w-]+|--[\w-]+)?\b|body\.tester-resizing\b)/,
        )
      }
    }
  })

  it('no shell module uses a class that is only in Backtest.css', () => {
    const shellClasses = classesOf(backtestShellCss)
    const panelOnly = [...classesOf(backtestCss)].filter((c) => !shellClasses.has(c))
    expect(panelOnly.length).toBeGreaterThan(50)
    for (const [f, src] of SOURCES) {
      if (f === 'components/BacktestPanel.tsx') continue
      for (const cls of panelOnly) {
        expect(new RegExp(`(?<![\\w-])${cls}(?![\\w-])`).test(src), `${f} uses .${cls}`).toBe(false)
      }
    }
  })
})

/** The first group (highest priority) whose test matches the module id. */
function groupOf(id: string): string | null {
  const byPriority = [...VENDOR_CHUNKS].sort((a, b) => b.priority - a.priority)
  return byPriority.find((g) => g.test.test(id))?.name ?? null
}

describe('vendor chunks (D2)', () => {
  it('are the rolldown codeSplitting groups of the build', () => {
    expect(viteConfig.build?.rolldownOptions?.output).toMatchObject({
      codeSplitting: { groups: VENDOR_CHUNKS },
    })
  })

  it.each([
    ['/w/node_modules/react/index.js', 'react'],
    ['/w/node_modules/react/jsx-runtime.js', 'react'],
    ['/w/node_modules/react-dom/client.js', 'react'],
    ['/w/node_modules/scheduler/cjs/scheduler.production.js', 'react'],
    ['C:\\w\\node_modules\\react-dom\\index.js', 'react'],
    [
      '/w/node_modules/lightweight-charts/dist/lightweight-charts.production.mjs',
      'lightweight-charts',
    ],
    ['/w/node_modules/fancy-canvas/index.mjs', 'lightweight-charts'],
    [
      '/w/node_modules/@tradingview/lwc-plugin-accessibility/dist/accessibility.js',
      'lwc-accessibility',
    ],
    ['/w/src/App.tsx', null],
    ['/w/src/components/ResearchPage.tsx', null],
    ['/w/node_modules/react-is/index.js', null],
    ['/w/node_modules/@fontsource/ibm-plex-sans/latin-400.css', null],
  ])('%s -> %s', (id, group) => {
    expect(groupOf(id)).toBe(group)
  })

  it('Lightweight Charts outranks the accessibility plugin that imports it', () => {
    // A group takes its modules' dependencies too: ranked lower, the plugin's group would
    // swallow the chart library.
    const p = Object.fromEntries(VENDOR_CHUNKS.map((g) => [g.name, g.priority]))
    expect(p['lightweight-charts']).toBeGreaterThan(p['lwc-accessibility'])
  })
})

describe('dev warm-up and fonts (D3)', () => {
  it('warms the two largest modules in dev', () => {
    expect(viteConfig.server?.warmup?.clientFiles).toEqual([
      './src/App.tsx',
      './src/chart/Chart.tsx',
    ])
  })

  it('keeps every weight the CSS uses', () => {
    for (const family of ['ibm-plex-sans', 'ibm-plex-mono']) {
      for (const weight of [400, 500, 600]) {
        expect(mainSource).toContain(`import '@fontsource/${family}/latin-${weight}.css'`)
      }
    }
  })

  it.each([
    ['ibm-plex-sans', sans400Css],
    ['ibm-plex-mono', mono400Css],
  ])('preloads %s 400 from the same file its stylesheet uses', (family, css) => {
    const file = `${family}-latin-400-normal.woff2`
    // The @font-face the stylesheet declares points at this file...
    expect(css).toContain(`url(./files/${file})`)
    // ...and index.html preloads that very file (Vite gives both the same hashed URL), as a CORS
    // font fetch, so the browser reuses the preload instead of downloading it twice.
    const link = indexHtml.match(
      new RegExp(`<link\\s+rel="preload"\\s+href="([^"]+${file})"([^>]*)/>`),
    )
    expect(link?.[1]).toBe(`/node_modules/@fontsource/${family}/files/${file}`)
    expect(link?.[2]).toMatch(/\bas="font"/)
    expect(link?.[2]).toMatch(/\btype="font\/woff2"/)
    expect(link?.[2]).toMatch(/\bcrossorigin\b/)
  })

  it('preloads only the two body faces', () => {
    expect(indexHtml.match(/rel="preload"/g)).toHaveLength(2)
  })
})
