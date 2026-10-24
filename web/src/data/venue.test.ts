import { describe, expect, it } from 'vitest'
import { VENUE_NOTE, venueNote } from '../components/backtestView'
import { noTradesCopy } from '../components/noTrades'
import { BinanceProvider } from './binance'
import { MarketDataError } from './provider'
import { ServerProvider } from './server'
import {
  BINANCE_REST,
  BINANCE_US_REST,
  BINANCE_US_WS,
  BINANCE_WS,
  DIRECT_VENUE,
  parseVenueInfo,
  VENUE_NAME,
  venueLabels,
} from './venue'

describe('market data venue', () => {
  it('is named "Binance" in the UI', () => {
    expect(VENUE_NAME).toBe('Binance')
  })

  it('uses the Binance market-data-only hosts', () => {
    expect(BINANCE_REST).toBe('https://data-api.binance.vision')
    expect(BINANCE_WS).toBe('wss://data-stream.binance.vision')
  })

  it('keeps the Binance.US hosts available as an alternative', () => {
    expect(BINANCE_US_REST).toBe('https://api.binance.us')
    expect(BINANCE_US_WS).toBe('wss://stream.binance.us:9443')
  })

  it('names the venue in the no-trades state', () => {
    const copy = noTradesCopy({
      symbol: 'SUIUSDT',
      interval: '1m',
      raw: [{ time: 60, open: 1, high: 1, low: 1, close: 1, volume: 0 }],
      exchange: VENUE_NAME,
      maxBars: 20_000,
      loadingOlder: false,
      historyExhausted: false,
      atLimit: false,
      retryBlocked: false,
      olderCooldown: false,
      offline: false,
    })
    expect(copy.title).toBe('No trades in this period on Binance')
  })
})

describe('venue labels', () => {
  it('reads "Binance · local server" once the server reports Binance', () => {
    expect(venueLabels({ id: 'binance', name: 'Binance' }, false)).toEqual({
      exchange: 'Binance',
      source: 'Binance · local server',
    })
  })

  it('names Binance.US when the server runs it', () => {
    expect(venueLabels({ id: 'binance-us', name: 'Binance.US' }, false).source).toBe(
      'Binance.US · local server',
    )
  })

  it('drops the transport in direct mode', () => {
    expect(venueLabels(DIRECT_VENUE, true)).toEqual({ exchange: 'Binance', source: 'Binance' })
  })

  it('claims no venue before the server answers', () => {
    expect(venueLabels(null, false)).toEqual({ exchange: 'the exchange', source: 'local server' })
  })
})

describe('parseVenueInfo', () => {
  it('accepts { id, name } and rejects anything else', () => {
    expect(parseVenueInfo({ id: 'binance', name: 'Binance', x: 1 })).toEqual({
      id: 'binance',
      name: 'Binance',
    })
    for (const bad of [null, [], 'binance', { id: 'binance' }, { id: '', name: 'B' }, { id: 1 }]) {
      expect(parseVenueInfo(bad)).toBeNull()
    }
  })
})

describe('getVenue', () => {
  const fetchJson = (body: unknown, urls: string[]) =>
    (async (input: RequestInfo | URL) => {
      urls.push(String(input))
      return new Response(JSON.stringify(body), { status: 200 })
    }) as typeof fetch

  it('ServerProvider reads the venue from /api/health', async () => {
    const urls: string[] = []
    const p = new ServerProvider({
      baseUrl: 'http://127.0.0.1:1',
      fetchImpl: fetchJson(
        { ok: true, db: true, upstreamSockets: 0, venue: { id: 'binance-us', name: 'Binance.US' } },
        urls,
      ),
    })
    await expect(p.getVenue()).resolves.toEqual({ id: 'binance-us', name: 'Binance.US' })
    expect(urls).toEqual(['http://127.0.0.1:1/api/health'])
  })

  it('ServerProvider rejects with parse when an older server sends no venue', async () => {
    const p = new ServerProvider({
      baseUrl: 'http://127.0.0.1:1',
      fetchImpl: fetchJson({ ok: true, db: true, upstreamSockets: 0 }, []),
    })
    const err = await p.getVenue().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(MarketDataError)
    expect((err as MarketDataError).kind).toBe('parse')
  })

  it('BinanceProvider reports Binance by default and a configured venue otherwise', async () => {
    await expect(new BinanceProvider().getVenue()).resolves.toEqual(DIRECT_VENUE)
    const us = { id: 'binance-us', name: 'Binance.US' }
    await expect(new BinanceProvider({ venue: us }).getVenue()).resolves.toEqual(us)
  })
})

describe('venueNote', () => {
  it('says same data only when the chart is on Binance global', () => {
    expect(venueNote({ id: 'binance', name: 'Binance' })).toBe(VENUE_NOTE)
    expect(VENUE_NOTE).toContain('same Binance spot data')
  })

  it('says the chart venue differs otherwise', () => {
    const note = venueNote({ id: 'binance-us', name: 'Binance.US' })
    expect(note).toContain('Binance.US')
    expect(note).toContain('differs from the backtest archive (Binance global spot')
    expect(note).not.toContain('same Binance spot data')
  })

  it('names only the archive while the venue is unknown', () => {
    const note = venueNote(null)
    expect(note).toContain('data.binance.vision')
    expect(note).not.toContain('same')
  })
})
