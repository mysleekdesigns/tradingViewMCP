import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// Each lazy panel names its export (`exportName`) so a retry after a failed chunk fetch can
// re-import the chunk under a fresh URL and take the component from it. A typo there would only
// surface on that rare retry, so check every pair against the real module here.
const source = readFileSync(fileURLToPath(new URL('./lazyPanels.tsx', import.meta.url)), 'utf8')
const pairs = [
  ...source.matchAll(
    /import\('\.\/(\w+)'\)\.then\(\(m\) => m\.(\w+)\),\s*\{ exportName: '(\w+)' \}/g,
  ),
].map(([, file, picked, exportName]) => ({ file, picked, exportName }))

describe('lazy panel export names', () => {
  it('covers all eight lazy panels', () => {
    expect(pairs).toHaveLength(8)
  })

  it.each(pairs)('$file exports $exportName, the same name load picks', async (p) => {
    expect(p.exportName).toBe(p.picked)
    const mod = (await import(`./${p.file}.tsx`)) as Record<string, unknown>
    expect(mod[p.exportName]).toBeDefined()
  })
})
