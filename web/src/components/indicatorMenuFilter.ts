import type { IndicatorDef, IndicatorId, Placement } from '../indicators/types'

/** Heading per placement, in the order groups appear in the menu. */
export const GROUP_LABELS: Readonly<Record<Placement, string>> = {
  overlay: 'Overlays',
  volume: 'Volume',
  pane: 'Oscillators',
}

const GROUP_ORDER: readonly Placement[] = ['overlay', 'volume', 'pane']

export interface IndicatorGroup {
  placement: Placement
  label: string
  items: readonly IndicatorDef[]
}

/**
 * Indicators matching `query` (case-insensitive substring of name, shortName, id or
 * description). Name/shortName prefix matches rank first; otherwise registry order is kept.
 * An empty or blank query returns every def in registry order.
 */
export function filterIndicators(
  defs: readonly IndicatorDef[],
  query: string,
): readonly IndicatorDef[] {
  const q = query.trim().toLowerCase()
  if (q === '') return defs
  const prefix: IndicatorDef[] = []
  const rest: IndicatorDef[] = []
  for (const d of defs) {
    const name = d.name.toLowerCase()
    const short = d.shortName.toLowerCase()
    if (name.startsWith(q) || short.startsWith(q)) {
      prefix.push(d)
    } else if (
      name.includes(q) ||
      short.includes(q) ||
      d.id.toLowerCase().includes(q) ||
      d.description.toLowerCase().includes(q)
    ) {
      rest.push(d)
    }
  }
  return [...prefix, ...rest]
}

/** Splits `defs` by placement (Overlays, Volume, Oscillators), keeping their order; empty groups are dropped. */
export function groupIndicators(defs: readonly IndicatorDef[]): readonly IndicatorGroup[] {
  return GROUP_ORDER.map((placement) => ({
    placement,
    label: GROUP_LABELS[placement],
    items: defs.filter((d) => d.placement === placement),
  })).filter((g) => g.items.length > 0)
}

/** The defs in the order they are shown (group by group), for keyboard navigation. */
export function flattenGroups(groups: readonly IndicatorGroup[]): readonly IndicatorDef[] {
  return groups.flatMap((g) => g.items)
}

/** How many instances of each type are active. */
export function countByType(
  items: readonly { type: IndicatorId }[],
): ReadonlyMap<IndicatorId, number> {
  const counts = new Map<IndicatorId, number>()
  for (const c of items) counts.set(c.type, (counts.get(c.type) ?? 0) + 1)
  return counts
}
