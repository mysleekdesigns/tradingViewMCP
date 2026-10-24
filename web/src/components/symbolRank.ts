import type { SymbolInfo } from '../types'

/** Quote assets the app lists, in display / tie-break order. */
export const PREFERRED_QUOTES: readonly string[] = ['USDT', 'USDC', 'BTC', 'USD']

/** Shown first when the query is empty. */
const POPULAR_BASES: readonly string[] = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE', 'ADA']

/** Uppercase and drop separators, so "btc/usdt", "BTC-USDT", and "btc usdt" all become "BTCUSDT". */
export function normalizeQuery(query: string): string {
  return query.toUpperCase().replace(/[\s/\-_.:]+/g, '')
}

/** Rank tier, lower is better; null means no match. */
export function matchTier(info: SymbolInfo, q: string): number | null {
  const symbol = info.symbol.toUpperCase()
  const base = info.baseAsset.toUpperCase()
  if (q === '') return 0
  if (symbol === q) return 0
  if (base === q) return 1
  if (symbol.startsWith(q)) return 2
  if (base.startsWith(q)) return 3
  if (symbol.includes(q)) return 4
  if (isSubsequence(q, symbol)) return 5
  return null
}

function isSubsequence(needle: string, hay: string): boolean {
  let i = 0
  for (let j = 0; j < hay.length && i < needle.length; j++) {
    if (hay[j] === needle[i]) i++
  }
  return i === needle.length
}

function quoteRank(quote: string): number {
  const i = PREFERRED_QUOTES.indexOf(quote.toUpperCase())
  return i === -1 ? PREFERRED_QUOTES.length : i
}

function popularRank(base: string): number {
  const i = POPULAR_BASES.indexOf(base.toUpperCase())
  return i === -1 ? POPULAR_BASES.length : i
}

export interface SearchOptions {
  /** Only symbols with this quote asset (case-insensitive). Omit for all. */
  quote?: string | null
}

/**
 * Pure, deterministic ranking: exact symbol > exact base asset > symbol prefix >
 * base-asset prefix > substring > subsequence. Ties: preferred quote order
 * (USDT, USDC, BTC, USD, others), then shorter symbol, then alphabetical.
 * An empty query lists popular bases first.
 */
export function searchSymbols(
  symbols: readonly SymbolInfo[],
  query: string,
  limit = 50,
  opts: SearchOptions = {},
): SymbolInfo[] {
  const q = normalizeQuery(query)
  const quote = opts.quote ? opts.quote.toUpperCase() : null
  const scored: { info: SymbolInfo; tier: number }[] = []
  for (const info of symbols) {
    if (quote !== null && info.quoteAsset.toUpperCase() !== quote) continue
    const tier = matchTier(info, q)
    if (tier !== null) scored.push({ info, tier })
  }
  scored.sort((a, b) => {
    if (a.tier !== b.tier) return a.tier - b.tier
    if (q === '') {
      const p = popularRank(a.info.baseAsset) - popularRank(b.info.baseAsset)
      if (p !== 0) return p
    }
    const qr = quoteRank(a.info.quoteAsset) - quoteRank(b.info.quoteAsset)
    if (qr !== 0) return qr
    if (a.info.symbol.length !== b.info.symbol.length) {
      return a.info.symbol.length - b.info.symbol.length
    }
    return a.info.symbol < b.info.symbol ? -1 : a.info.symbol > b.info.symbol ? 1 : 0
  })
  return scored.slice(0, Math.max(0, limit)).map((s) => s.info)
}

/** Quote assets present in `symbols`, preferred ones first, then alphabetical. */
export function quoteAssetsOf(symbols: readonly SymbolInfo[]): string[] {
  const set = new Set(symbols.map((s) => s.quoteAsset.toUpperCase()))
  return [...set].sort((a, b) => quoteRank(a) - quoteRank(b) || (a < b ? -1 : a > b ? 1 : 0))
}

/** Symbols the app offers: currently trading, quoted in one of `PREFERRED_QUOTES`. */
export function tradableSymbols(all: readonly SymbolInfo[]): SymbolInfo[] {
  return all.filter(
    (s) => s.status === 'TRADING' && PREFERRED_QUOTES.includes(s.quoteAsset.toUpperCase()),
  )
}
