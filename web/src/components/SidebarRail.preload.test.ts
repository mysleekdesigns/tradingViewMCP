import { isValidElement, type ReactElement, type ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import railSource from './SidebarRail.tsx?raw'

// Each heavy module records when it is first evaluated, i.e. when its chunk would be fetched,
// and whether that load counted as a real open (`panelLoadInFlight`, which main.tsx's reload
// handler checks).
const loaded = vi.hoisted(() => [] as string[])
const counted = vi.hoisted(() => [] as boolean[])
async function record(name: string) {
  const { panelLoadInFlight } = await import('./lazyLoad')
  loaded.push(name)
  counted.push(panelLoadInFlight())
  return {}
}
vi.mock('./BacktestPanel', () => record('BacktestPanel'))
vi.mock('./ResearchPage', () => record('ResearchPage'))
vi.mock('./PaperTradingPanel', () => record('PaperTradingPanel'))

const { SidebarRail } = await import('./SidebarRail')

type ButtonProps = {
  'aria-label': string
  onPointerEnter?(): void
  onFocus?(): void
  children?: ReactNode
}

function buttons(): Map<string, ButtonProps> {
  const inner = (SidebarRail as unknown as { type(p: unknown): ReactElement }).type
  const tree = inner({
    active: null,
    onSelect: () => {},
    alertBadge: 0,
    testerOpen: false,
    onToggleTester: () => {},
    researchOpen: false,
    onToggleResearch: () => {},
  })
  const out = new Map<string, ButtonProps>()
  const walk = (node: ReactNode): void => {
    if (Array.isArray(node)) return node.forEach(walk)
    if (!isValidElement(node)) return
    const props = node.props as ButtonProps
    if (node.type === 'button') out.set(props['aria-label'], props)
    walk(props.children)
  }
  walk(tree)
  return out
}

async function settle(): Promise<void> {
  await vi.dynamicImportSettled()
}

describe('SidebarRail preloads lazy panels on intent (Phase 19, D1)', () => {
  it('loads nothing heavy until a button is pointed at or focused', async () => {
    const rail = buttons()
    await settle()
    expect(loaded).toEqual([])
    // The always-loaded panels have no preload.
    rail.get('Watchlist')?.onPointerEnter?.()
    rail.get('Alerts')?.onFocus?.()
    expect(rail.get('Watchlist')?.onPointerEnter).toBeUndefined()
    await settle()
    expect(loaded).toEqual([])
  })

  it('hover or focus starts the matching panel download', async () => {
    const rail = buttons()
    rail.get('Strategy Tester')?.onPointerEnter?.()
    await settle()
    expect(loaded).toEqual(['BacktestPanel'])
    rail.get('Research')?.onFocus?.()
    await settle()
    expect(loaded).toEqual(['BacktestPanel', 'ResearchPage'])
    rail.get('Paper trading')?.onPointerEnter?.()
    await settle()
    expect(loaded).toEqual(['BacktestPanel', 'ResearchPage', 'PaperTradingPanel'])
    // Again: the module cache answers, no second load.
    rail.get('Strategy Tester')?.onFocus?.()
    await settle()
    expect(loaded).toHaveLength(3)
  })

  it('a preload is speculative: it never counts as a real open, so a failure cannot reload', () => {
    // Runs after the downloads above: every one of them happened with no real open in flight.
    expect(counted).toEqual([false, false, false])
  })

  it('imports the panels only dynamically (a static import would put them in the main chunk)', () => {
    for (const mod of ['BacktestPanel', 'ResearchPage', 'PaperTradingPanel']) {
      expect(railSource).not.toMatch(new RegExp(`from '\\./${mod}'`))
      expect(railSource).toContain(`import('./${mod}')`)
    }
    expect(railSource).toMatch(/import \{ ResearchIcon, TesterIcon \} from '\.\/icons'/)
  })
})
