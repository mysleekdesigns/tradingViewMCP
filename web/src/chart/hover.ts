// What the mouse is over, from Lightweight Charts 5.2 hit testing (`MouseEventParams.hoveredInfo`).
//
// LWC resolves the topmost item under the cursor itself (series lines/bars within each series'
// `hitTestTolerance`, price lines, markers, and primitives through their `hitTest`). This turns
// that into the app's terms: an indicator (by config uid) or a drawing (by id). The drawings
// primitive reports `externalId: 'drawing:<id>'`, which LWC passes on as `hoveredInfo.objectId`.

import type { ISeriesApi, MouseEventParams, SeriesType, Time } from 'lightweight-charts'

export type HoverTarget =
  { kind: 'indicator'; uid: string } | { kind: 'drawing'; id: string } | null

/** Prefix of the drawings primitive's hit-test `externalId`. */
export const DRAWING_OBJECT_PREFIX = 'drawing:'

/** Prefix of a range indicator primitive's hit-test `externalId` (volume profile). */
export const INDICATOR_OBJECT_PREFIX = 'indicator:'

/** The hit-test id the drawings primitive reports for drawing `id`. */
export function drawingObjectId(id: string): string {
  return `${DRAWING_OBJECT_PREFIX}${id}`
}

/**
 * The indicator or drawing under the cursor, or null (nothing, the main series, volume, or any
 * other series or primitive). `uidOfSeries` maps an indicator's series to its config uid.
 */
export function resolveHover(
  param: Pick<MouseEventParams<Time>, 'hoveredInfo'>,
  uidOfSeries: (series: ISeriesApi<SeriesType>) => string | null,
): HoverTarget {
  const info = param.hoveredInfo
  if (!info) return null
  if (info.sourceKind === 'series' && info.series) {
    const uid = uidOfSeries(info.series)
    if (uid !== null) return { kind: 'indicator', uid }
  }
  const objectId = info.objectId
  if (typeof objectId === 'string' && objectId.startsWith(DRAWING_OBJECT_PREFIX)) {
    const id = objectId.slice(DRAWING_OBJECT_PREFIX.length)
    if (id.length > 0) return { kind: 'drawing', id }
  }
  if (typeof objectId === 'string' && objectId.startsWith(INDICATOR_OBJECT_PREFIX)) {
    const uid = objectId.slice(INDICATOR_OBJECT_PREFIX.length)
    if (uid.length > 0) return { kind: 'indicator', uid }
  }
  return null
}

/** Same target (both null, or same kind and id). Lets callers skip redundant updates. */
export function sameHover(a: HoverTarget, b: HoverTarget): boolean {
  if (a === null || b === null) return a === b
  if (a.kind === 'indicator') return b.kind === 'indicator' && a.uid === b.uid
  return b.kind === 'drawing' && a.id === b.id
}
