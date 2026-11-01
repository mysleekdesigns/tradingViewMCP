import { describe, expect, it } from 'vitest'

// Phase 19 wave 5: components/primitives.css holds the chrome the component stylesheets used to
// repeat (icon button, tabs, dialog shell, focus ring, thin scrollbar, panel head/title/empty).
// These tests keep it loaded first, keep the copies from creeping back, and keep its selectors
// pointed at classes the components actually render.

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
const SRC = new URL('../', import.meta.url)
const read = (path: string) => fs.readFileSync(new URL(path, SRC), 'utf8')
const files = fs.readdirSync(SRC, { recursive: true }).map((f) => f.replace(/\\/g, '/'))
const STYLESHEETS = files.filter((f) => f.endsWith('.css') && f !== 'components/primitives.css')
const TSX = files
  .filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
  .map((f) => read(f))
  .join('\n')
const primitivesCss = read('components/primitives.css')
const indexCss = read('index.css')

interface Rule {
  /** Enclosing at-rules, outermost first ('' at the top level). */
  context: string
  selectors: string[]
  decls: { prop: string; value: string }[]
}

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const norm = (s: string) => s.replace(/\s+/g, ' ').trim()

/** Splits on top-level commas (not inside `:not(...)` or `[...]`). */
function splitSelectors(list: string): string[] {
  const out: string[] = []
  let depth = 0
  let cur = ''
  for (const ch of list) {
    if (ch === '(' || ch === '[') depth++
    if (ch === ')' || ch === ']') depth--
    if (ch === ',' && depth === 0) {
      out.push(norm(cur))
      cur = ''
    } else cur += ch
  }
  out.push(norm(cur))
  return out.filter(Boolean)
}

/** A small parser for this repo's stylesheets: style rules, @media nesting, @keyframes skipped. */
function parseRules(css: string): Rule[] {
  const rules: Rule[] = []
  const src = stripComments(css)
  const walk = (body: string, context: string) => {
    let i = 0
    while (i < body.length) {
      const open = body.indexOf('{', i)
      if (open < 0) return
      const prelude = norm(body.slice(i, open).replace(/^[\s\S]*;/, ''))
      let depth = 1
      let j = open + 1
      while (j < body.length && depth > 0) {
        if (body[j] === '{') depth++
        else if (body[j] === '}') depth--
        j++
      }
      const inner = body.slice(open + 1, j - 1)
      if (prelude.startsWith('@media') || prelude.startsWith('@supports')) {
        walk(inner, norm(`${context} ${prelude}`))
      } else if (!prelude.startsWith('@')) {
        const decls = inner
          .split(';')
          .map((d) => d.trim())
          .filter((d) => d.includes(':'))
          .map((d) => {
            const at = d.indexOf(':')
            return { prop: d.slice(0, at).trim(), value: norm(d.slice(at + 1)) }
          })
        rules.push({ context, selectors: splitSelectors(prelude), decls })
      }
      i = j
    }
  }
  walk(src, '')
  return rules
}

const primitives = parseRules(primitivesCss)
/** selector -> property -> value, as primitives.css resolves it (later blocks win). */
const resolved = new Map<string, Map<string, string>>()
for (const rule of primitives) {
  for (const sel of rule.selectors) {
    const props = resolved.get(sel) ?? new Map<string, string>()
    for (const d of rule.decls) props.set(d.prop, d.value)
    resolved.set(sel, props)
  }
}

describe('primitives.css loads before every component stylesheet', () => {
  it('is the first statement of index.css, which main.tsx imports before the app', () => {
    expect(stripComments(indexCss).trimStart()).toMatch(
      /^@import '\.\/components\/primitives\.css';/,
    )
    expect(read('main.tsx')).toContain("import './index.css'")
  })

  it('no other module imports it, so its place in the cascade cannot move', () => {
    const importers = files
      .filter((f) => /\.(tsx?|css)$/.test(f) && !/\.test\.tsx?$/.test(f))
      .filter((f) => /(@import|import)\s+['"][^'"]*primitives\.css['"]/.test(read(f)))
    expect(importers).toEqual(['index.css'])
  })

  it('is flat: no @media, @keyframes or nesting that could reorder against the components', () => {
    expect(stripComments(primitivesCss)).not.toMatch(/@/)
    for (const rule of primitives) expect(rule.context).toBe('')
  })
})

describe('primitives.css selects classes the components render', () => {
  const classes = new Set(
    [...stripComments(primitivesCss).matchAll(/\.([a-z][\w-]*)/g)].map((m) => m[1]),
  )

  it.each([...classes])('.%s is used by a component', (cls) => {
    // A modifier may be built as `${block}--${state}`; its block-- prefix then appears instead.
    const used =
      new RegExp(`(^|[^\\w-])${cls}(?![\\w-])`).test(TSX) ||
      (cls.includes('--') && TSX.includes(cls.slice(0, cls.indexOf('--') + 2)))
    expect(used).toBe(true)
  })
})

describe('the shared declarations are not copied back into the components', () => {
  it.each(STYLESHEETS)('%s repeats no primitive declaration for the same selector', (file) => {
    const copies: string[] = []
    for (const rule of parseRules(read(file))) {
      if (rule.context !== '') continue
      for (const d of rule.decls) {
        if (rule.selectors.every((sel) => resolved.get(sel)?.get(d.prop) === d.value)) {
          copies.push(`${rule.selectors.join(', ')} { ${d.prop}: ${d.value} }`)
        }
      }
    }
    expect(copies).toEqual([])
  })
})

describe('focus ring', () => {
  const ringRule = primitives.find((r) =>
    r.decls.some((d) => d.prop === 'outline' && d.value === '2px solid var(--accent)'),
  )

  it('is one rule, and no stylesheet draws the same 2px accent ring itself', () => {
    expect(ringRule).toBeDefined()
    expect(
      primitives.filter((r) => r.decls.some((d) => d.prop === 'outline' && d.value !== 'none')),
    ).toHaveLength(1)
    for (const file of STYLESHEETS) {
      expect(stripComments(read(file))).not.toMatch(/outline:\s*2px solid var\(--accent\)/)
    }
  })

  it('every selector given an offset here also gets the ring', () => {
    const ringed = new Set(ringRule?.selectors)
    for (const rule of primitives) {
      if (!rule.decls.some((d) => d.prop === 'outline-offset')) continue
      for (const sel of rule.selectors) expect(ringed).toContain(sel)
    }
  })

  it('only keyboard focus draws it (:focus-visible or :focus-within)', () => {
    for (const sel of ringRule?.selectors ?? []) expect(sel).toMatch(/:focus-(visible|within)$/)
  })
})

describe('reduced motion still covers the primitives', () => {
  it('index.css switches every animation and transition off, !important, for reduce', () => {
    const reduce = parseRules(indexCss).filter((r) =>
      r.context.includes('prefers-reduced-motion: reduce'),
    )
    const universal = reduce.find((r) => r.selectors.includes('*'))
    expect(universal?.decls).toEqual(
      expect.arrayContaining([
        { prop: 'animation', value: 'none !important' },
        { prop: 'transition', value: 'none !important' },
      ]),
    )
  })
})
