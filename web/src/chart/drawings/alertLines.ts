// Active alerts as dashed price lines on the main series. Diffed by id so a changed price only
// moves its line, and re-created on the new series after a chart-type switch.

import type { CreatePriceLineOptions, IPriceLine } from 'lightweight-charts'
import type { AlertLine } from './types'

export interface AlertLinesDiff {
  add: AlertLine[]
  update: AlertLine[]
  remove: string[]
}

/** What changes turn the lines currently shown (`prev`, by id) into `next`. */
export function diffAlertLines(
  prev: ReadonlyMap<string, AlertLine>,
  next: readonly AlertLine[],
): AlertLinesDiff {
  const add: AlertLine[] = []
  const update: AlertLine[] = []
  const seen = new Set<string>()
  for (const line of next) {
    if (seen.has(line.id)) continue
    seen.add(line.id)
    const old = prev.get(line.id)
    if (!old) add.push(line)
    else if (old.price !== line.price || old.title !== line.title) update.push(line)
  }
  const remove = [...prev.keys()].filter((id) => !seen.has(id))
  return { add, update, remove }
}

/** The part of a series this manager uses (keeps it testable with a fake). */
export interface PriceLineHost {
  createPriceLine(options: CreatePriceLineOptions): IPriceLine
  removePriceLine(line: IPriceLine): void
}

export class AlertLinesManager {
  private host: PriceLineHost | null = null
  private readonly shown = new Map<string, { alert: AlertLine; line: IPriceLine }>()
  private current: readonly AlertLine[] = []
  private readonly options: (price: number, title: string) => CreatePriceLineOptions

  constructor(options: (price: number, title: string) => CreatePriceLineOptions) {
    this.options = options
  }

  /** Draw the current alerts on a (new) main series. */
  attach(host: PriceLineHost): void {
    this.detach(true)
    this.host = host
    this.apply()
  }

  /**
   * Forget the series. `removeLines` false when the chart (and its lines) is already gone.
   */
  detach(removeLines: boolean): void {
    const host = this.host
    if (host && removeLines) for (const { line } of this.shown.values()) host.removePriceLine(line)
    this.shown.clear()
    this.host = null
  }

  sync(lines: readonly AlertLine[]): void {
    this.current = lines
    this.apply()
  }

  /** Ids currently drawn (for tests). */
  ids(): string[] {
    return [...this.shown.keys()]
  }

  private apply(): void {
    const host = this.host
    if (!host) return
    const prev = new Map([...this.shown].map(([id, v]) => [id, v.alert]))
    const { add, update, remove } = diffAlertLines(prev, this.current)
    for (const id of remove) {
      const entry = this.shown.get(id)
      if (entry) host.removePriceLine(entry.line)
      this.shown.delete(id)
    }
    for (const alert of update) {
      const entry = this.shown.get(alert.id)
      if (!entry) continue
      entry.line.applyOptions({ price: alert.price, title: alert.title })
      entry.alert = alert
    }
    for (const alert of add) {
      const line = host.createPriceLine(this.options(alert.price, alert.title))
      this.shown.set(alert.id, { alert, line })
    }
  }
}
