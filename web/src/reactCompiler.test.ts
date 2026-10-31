// Slow block (runs the compiler over src/, several seconds): SLOW=1 npx vitest run src/reactCompiler.test.ts
import { describe, expect, it } from 'vitest'
import viteConfig, {
  REACT_COMPILER_EXCLUDE,
  REACT_COMPILER_MODE,
  reactCompiler,
} from '../vite.config'

// Phase 19 wave 4 (D4): the React Compiler runs in the build through `@rolldown/plugin-babel`.
// These tests use the very preset the build uses, so a change to its mode or filter shows here.

interface FsLike {
  readFileSync(p: URL, enc: 'utf8'): string
  readdirSync(p: URL, o: { recursive: true }): string[]
}
interface BabelLike {
  transformAsync(
    code: string,
    opts: Record<string, unknown>,
  ): Promise<{ code?: string | null } | null>
}
interface CompilerEvent {
  kind: string
  fnName?: string | null
  fnLoc?: { start: { line: number } } | null
}

// The app tsconfig has no Node types and `@babel/core` ships none, hence the narrow local types.
const fs = (
  globalThis as unknown as { process: { getBuiltinModule(id: 'node:fs'): FsLike } }
).process.getBuiltinModule('node:fs')
const BABEL: string = '@babel/core'
// Loaded on first use, so the default run (which skips every compile) does not pay for Babel.
let babel: BabelLike | null = null

const SRC = new URL('./', import.meta.url)
const read = (rel: string) => fs.readFileSync(new URL(rel, SRC), 'utf8')

const preset = (reactCompiler.preset as () => { plugins: [string, Record<string, unknown>][] })()
const [compilerPath, compilerOptions] = preset.plugins[0]

/** Runs the build's compiler plugin (options as configured, plus a logger) over one file. */
async function compile(rel: string, mode: string = REACT_COMPILER_MODE) {
  babel ??= (await import(/* @vite-ignore */ BABEL)) as BabelLike
  const events: CompilerEvent[] = []
  const out = await babel.transformAsync(read(rel), {
    filename: rel,
    babelrc: false,
    configFile: false,
    parserOpts: { plugins: ['typescript', 'jsx'] },
    plugins: [
      [
        compilerPath,
        {
          ...compilerOptions,
          compilationMode: mode,
          logger: { logEvent: (_f: string | null, e: CompilerEvent) => events.push(e) },
        },
      ],
    ],
  })
  return {
    code: out?.code ?? '',
    compiled: events.filter((e) => e.kind === 'CompileSuccess').map((e) => e.fnName),
    failed: events.filter((e) => e.kind !== 'CompileSuccess' && e.kind !== 'Timing'),
  }
}

describe('build config', () => {
  it('compiles in \'annotation\' mode: only functions marked "use memo"', () => {
    expect(REACT_COMPILER_MODE).toBe('annotation')
    expect(compilerOptions).toMatchObject({ compilationMode: 'annotation' })
    expect(compilerPath).toMatch(/babel-plugin-react-compiler/)
    // React 19 is the default target (react/compiler-runtime, no polyfill package).
    expect(compilerOptions).not.toHaveProperty('target')
    expect(reactCompiler.rolldown.optimizeDeps?.include).toEqual(['react/compiler-runtime'])
  })

  it('only hands files containing the "use memo" directive to Babel', () => {
    const code = reactCompiler.rolldown.filter?.code as RegExp
    expect(code.test("function Header() {\n  'use memo'\n}")).toBe(true)
    expect(code.test('function Header() {\n  "use memo"\n}')).toBe(true)
    expect(code.test('export function Header() {}')).toBe(false)
    expect(code.test('export function useReplay() {}')).toBe(false)
  })

  it('applies to the browser build only; Vitest (Node) runs the source as written', () => {
    const applies = reactCompiler.rolldown.applyToEnvironmentHook
    expect(applies?.({ config: { consumer: 'client' } } as never)).toBe(true)
    expect(applies?.({ config: { consumer: 'server' } } as never)).toBe(false)
  })

  it('leaves chart/Chart.tsx as written and compiles every other module', () => {
    expect(reactCompiler.rolldown.filter?.id).toEqual({ exclude: REACT_COMPILER_EXCLUDE })
    const excluded = (id: string) => REACT_COMPILER_EXCLUDE.some((re) => re.test(id))
    expect(excluded('/w/src/chart/Chart.tsx')).toBe(true)
    expect(excluded('C:\\w\\src\\chart\\Chart.tsx')).toBe(true)
    expect(excluded('/w/src/chart/Chart.tsx?v=1')).toBe(true)
    expect(excluded('/w/src/chart/IndicatorLegend.tsx')).toBe(false)
    expect(excluded('/w/src/components/ChartPanel.tsx')).toBe(false)
    expect(excluded('/w/src/App.tsx')).toBe(false)
  })

  it('the babel plugin with that preset runs after the React plugin', async () => {
    const plugins = (await Promise.all((viteConfig.plugins ?? []).flat(2))) as {
      name?: string
    }[]
    const names = plugins.map((p) => p?.name)
    expect(names).toContain('@rolldown/plugin-babel')
    expect(names.indexOf('@rolldown/plugin-babel')).toBeGreaterThan(
      names.indexOf('vite:react-babel'),
    )
  })
})

const SHELL: [string, string[]][] = [
  ['components/Header.tsx', ['Header']],
  ['components/Watchlist.tsx', ['Watchlist', 'WatchlistRow']],
  ['components/StatusBar.tsx', ['StatusBar']],
  ['components/SidebarRail.tsx', ['SidebarRail']],
  ['components/DrawingToolbar.tsx', ['DrawingToolbar']],
  ['components/AlertsPanel.tsx', ['AlertsPanel']],
  ['components/QuoteReadout.tsx', ['QuoteView', 'QuoteReadout']],
  ['components/LastUpdateClock.tsx', ['LastUpdateText', 'LastUpdateClock']],
  ['App.tsx', ['LiveHeader', 'LiveStatusBar']],
]

describe('the shell carries the directive', () => {
  it.each(SHELL)('%s: one "use memo" per function in %j', (rel, fns) => {
    const src = read(rel)
    expect(src.match(/^\s+'use memo'$/gm)).toHaveLength(fns.length)
    for (const fn of fns) {
      // The directive is the first statement of that function's body.
      expect(src).toMatch(
        new RegExp(`memo\\(function ${fn}\\([\\s\\S]*?\\) \\{\\n\\s+'use memo'\\n`),
      )
    }
  })
})

// Everything below runs babel-plugin-react-compiler over source files: slow, so opt-in.
describe.skipIf(!import.meta.env.SLOW)('compiler output (slow)', () => {
  describe('the shell compiles', () => {
    it.each(SHELL)(
      '%s: %j are marked "use memo" and compile in annotation mode',
      async (rel, fns) => {
        const { compiled, failed } = await compile(rel, 'annotation')
        expect(failed).toEqual([])
        expect([...compiled].sort()).toEqual([...fns].sort())
      },
    )

    it.each(SHELL)('%s: %j compile in the configured mode', async (rel, fns) => {
      const { compiled, failed } = await compile(rel)
      expect(failed).toEqual([])
      for (const fn of fns) expect(compiled).toContain(fn)
    })

    it('in annotation mode nothing outside the shell is compiled', async () => {
      const files = fs
        .readdirSync(SRC, { recursive: true })
        .filter((f) => /\.tsx?$/.test(f) && !/\.test\.|\.d\.ts$|testkit/.test(f))
      const compiled: string[] = []
      for (const rel of files) {
        const r = await compile(rel, 'annotation')
        expect(r.failed).toEqual([])
        compiled.push(...r.compiled.map(String))
      }
      expect(compiled.sort()).toEqual(SHELL.flatMap(([, fns]) => fns).sort())
    })

    it('infer mode would also compile App, its drawing stores and the replay hook', async () => {
      expect((await compile('App.tsx', 'infer')).compiled).toEqual(
        expect.arrayContaining(['App', 'useDrawingStores', 'LiveAlertDialog']),
      )
      expect((await compile('state/useReplay.ts', 'infer')).compiled).toContain('useReplay')
    })
  })

  /** Files whose functions the compiler skips (each such function runs exactly as written). */
  const KNOWN_BAILOUTS = [
    // try/finally and a counter updated inside a callback: not supported by the compiler yet.
    'chart/Chart.tsx',
    // try/finally in the submit handler.
    'components/AlertDialog.tsx',
    // A `{ [x as K]: … }` computed key.
    'components/AlertToasts.tsx',
  ]

  describe('bailouts across src in infer mode (readiness for widening the mode)', () => {
    it('only the known functions are skipped', async () => {
      const files = fs
        .readdirSync(SRC, { recursive: true })
        .filter((f) => /\.tsx?$/.test(f) && !/\.test\.|\.d\.ts$|testkit/.test(f))
      const skipped = new Set<string>()
      for (const rel of files) {
        if ((await compile(rel, 'infer')).failed.length > 0) skipped.add(rel.replaceAll('\\', '/'))
      }
      expect([...skipped].sort()).toEqual([...KNOWN_BAILOUTS].sort())
    }, 60_000)
  })

  describe('state/drawings.ts', () => {
    it('has no rule suppression, so useDrawings compiles', async () => {
      expect(read('state/drawings.ts')).not.toMatch(/eslint-disable/)
      expect((await compile('state/drawings.ts', 'infer')).compiled).toContain('useDrawings')
    })

    it('the compiled mirror read still re-runs when the client changes, not only the symbol', async () => {
      const { code } = await compile('state/drawings.ts', 'infer')
      const m = code.match(/if \(([^)]*)\) \{\s*(\w+) = readMirrorForClient\(sym, client\);/)
      expect(m).not.toBeNull()
      const guard = m?.[1] ?? ''
      expect(guard).toMatch(/\$\[\d+\] !== client/)
      expect(guard).toMatch(/\$\[\d+\] !== sym/)
    })
  })

  describe('render-time reads the compiler cannot see', () => {
    it("App's dock height follows the viewport of the current render, as before", async () => {
      const { code } = await compile('App.tsx', 'infer')
      const m = code.match(/if \(([^)]*)\) \{\s*\w+ = clampDockHeight\(/)
      expect(m).not.toBeNull()
      // Keyed on the stored height and the viewport (a hook result), not on the height alone.
      expect(m?.[1].match(/!==/g)).toHaveLength(2)
      expect(m?.[1]).toMatch(/dock\.height/)
      expect(code).toContain('useViewportHeight()')
    })

    it('compiled, the live legend rows would cache the indicator read (why Chart.tsx is excluded)', async () => {
      const { code } = await compile('chart/Chart.tsx', 'infer')
      // The store subscription's snapshot is unused, so nothing in the memo key moves on a tick.
      expect(code).toMatch(
        /if \(\$\[\d+\] !== hoveredTime \|\| \$\[\d+\] !== intervalSeconds \|\| \$\[\d+\] !== manager \|\| \$\[\d+\] !== render\) \{\s*\w+ = render\(manager\.legendPartition\(/,
      )
    })
  })

  describe("why not 'all'", () => {
    it('would put the memo-cache hook into plain helpers that run outside React', async () => {
      const all = await compile('indicators/bollinger.ts', 'all')
      expect(all.code).toMatch(/function bbStepper\([^)]*\) \{\s*const \$ = _c\(\d+\)/)
      const inferred = await compile('indicators/bollinger.ts', 'infer')
      expect(inferred.compiled).toEqual([])
      expect(inferred.code).not.toMatch(/_c\(/)
    })
  })
})
