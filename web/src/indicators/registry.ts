import { anchoredVwapDef } from './anchoredVwap'
import { bollingerDef } from './bollinger'
import { emaDef } from './ema'
import { macdDef } from './macd'
import { marketStructureDef } from './marketStructure'
import { rsiDef } from './rsi'
import { smaDef } from './sma'
import { stochDef } from './stochastic'
import type { IndicatorDef, IndicatorId } from './types'
import { volumeMaDef } from './volumeMa'
import { volumeProfileDef } from './volumeProfile'
import { vwapDef } from './vwap'

/** Menu order: overlays first, then volume, then pane oscillators. */
export const INDICATORS: readonly IndicatorDef[] = [
  smaDef,
  emaDef,
  bollingerDef,
  vwapDef,
  anchoredVwapDef,
  volumeProfileDef,
  marketStructureDef,
  volumeMaDef,
  rsiDef,
  macdDef,
  stochDef,
]

export const INDICATOR_BY_ID: ReadonlyMap<IndicatorId, IndicatorDef> = new Map(
  INDICATORS.map((d) => [d.id, d]),
)

export function getIndicatorDef(id: IndicatorId): IndicatorDef | undefined {
  return INDICATOR_BY_ID.get(id)
}
