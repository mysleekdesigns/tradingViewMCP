import { memo, useCallback, useEffect, useMemo, type ReactNode } from 'react'
import type { BacktestChartData, FocusPlan } from '../chart/backtestOverlay'
import { Chart, type ChartApi } from '../chart/Chart'
import { regimeSourceBars } from '../chart/regimeRibbon'
import type { CrosshairSync } from '../chart/crosshairSync'
import type { ChartContextMenuRequest, DrawingStyle, ToolMode } from '../chart/drawings/types'
import type { ChartAppearance } from '../chart/theme'
import type { Alert } from '../data/alertTypes'
import {
  LAB_OFFLINE_MESSAGE,
  type LabDerivativesClient,
  type LabRegimeClient,
} from '../data/labApi'
import { MarketDataError } from '../data/provider'
import type { CandleSource } from '../data/timeframeProvider'
import { timeframeSeconds } from '../data/timeframes'
import { alertLinesFor } from '../state/chartTools'
import type { DrawingsApi } from '../state/drawings'
import type { IndicatorConfig } from '../state/indicators'
import type { PanelState } from '../state/layout'
import { atHistoryLimit } from '../state/liveCandles'
import { REGIME_THRESHOLD, REGIME_WINDOW_BARS, regimeParamsLabel } from '../state/regimePref'
import { useLiveCandles, type UseLiveCandles } from '../state/useLiveCandles'
import { regimeSupported, useRegime, type RegimeOverlay } from '../state/useRegime'
import {
  derivativesArchiveEnd,
  derivativesUnsupportedReason,
  quotedCommand,
  useDerivatives,
  type DerivativesState,
} from '../state/useDerivatives'
import type { Candle, Timeframe } from '../types'
import './AppShell.css'
import './Derivatives.css'
import { MAX_HISTORY_BARS } from './backtestView'
import { ConnectionIndicator } from './ConnectionStatus'
import './Layout.css'
import { NoTradesView } from './NoTradesView'
import { ErrorView, LoadingView } from './StatusViews'

/**
 * One chart cell of the multi-chart layout. It owns its panel's live data (its own
 * `useLiveCandles`, so each panel has its own socket, closed on unmount or symbol/interval
 * change) and its `<Chart>`; App owns every piece of global state and passes it down.
 *
 * Callbacks take the panel id so App can pass one stable function to every panel. `onLive`
 * hands the panel's live candles up (paper-trading feed, quote, bar replay source); bar replay
 * comes back down as `candlesOverride`; the regime ribbon is fetched here from the panel's
 * loaded bars and drawn on the ones it plots.
 *
 * Memoised: App re-renders on every tick of any panel, so every prop App passes must be stable
 * between ticks of other panels (callbacks, memoised arrays and elements).
 */
/** The market a live report belongs to. */
export interface PanelMarket {
  symbol: string
  interval: Timeframe
}

export interface ChartPanelProps {
  panel: PanelState
  /** Candles for any Timeframe (a TimeframeProvider; a MarketDataProvider for native ones). */
  provider: CandleSource
  /**
   * The provider for this timeframe is not known yet (seconds charts while the local server has
   * not reported its venue): show a loading state instead of asking `provider`.
   */
  venuePending?: boolean
  /** First REST page size for `useLiveCandles`. */
  limit: number
  /** Source name inside chart error messages (stable for the app's lifetime). */
  errorSource: string
  /** Legend / loading label source, e.g. "Binance · local server". */
  source: string
  /** Venue name for the no-trades view, e.g. "Binance". */
  exchange: string
  /** This panel takes the toolbar, the symbol search and the keyboard. */
  active: boolean
  /** More than one panel is shown: draw the cell frame and the compact header strip. */
  multi: boolean
  /** A press or focus inside the cell. */
  onActivate: (panelId: string) => void
  /**
   * The panel's latest live state (candles in `load`, connection status, reload, loadOlder)
   * after every render, with the market it belongs to, and null on unmount. The object is fresh
   * per render; compare it shallowly before storing it. Right after a symbol or timeframe
   * change the panel's previous report is still stored for one render: check `market` before
   * using the candles for the new market.
   */
  onLive?: (panelId: string, live: UseLiveCandles | null, market?: PanelMarket) => void
  /** The mounted chart's API (screenshots), null while no chart is shown. */
  onApi?: (panelId: string, api: ChartApi | null) => void
  appearance: ChartAppearance
  /** Indicator configs; one shared list applies to every panel. */
  indicators: readonly IndicatorConfig[]
  onIndicatorToggleVisible: (uid: string) => void
  onIndicatorRemove: (uid: string) => void
  onIndicatorSettings: (uid: string) => void
  /** The drawings store of this panel's symbol (shared by panels on the same symbol). */
  drawings: DrawingsApi
  tool: ToolMode
  onToolDone: () => void
  drawingStyle: DrawingStyle
  /** The selected drawing when it belongs to this panel, else null. */
  selectedDrawingId: string | null
  onSelectDrawing: (panelId: string, id: string | null) => void
  onContextMenu: (panelId: string, request: ChartContextMenuRequest) => void
  /** Every alert; the panel draws the active ones for its own symbol. */
  alerts: readonly Alert[]
  /** Backtest trades + equity, only for the panel the report belongs to. */
  backtest: BacktestChartData | null
  focusRange: { key: number; from: number; to: number } | null
  onFocusDone: (key: number, result: FocusPlan['kind']) => void
  /** A dialog or menu has the keyboard (non-active panels are blocked as well). */
  keyboardBlocked: boolean
  /** Crosshair sync group, or null when sync is off. */
  crosshairSync: CrosshairSync | null
  /**
   * Bars to plot instead of the live ones (bar replay: closed bars up to the cursor). The live
   * stream keeps running underneath; indicators and the regime ribbon use these bars only.
   */
  candlesOverride?: readonly Candle[] | null
  /** A bar replay session is open on this panel (amber frame + strip tag), in any state. */
  replaySession?: boolean
  /** Left click on the main pane with the cursor tool (bar open time, or null); replay picking. */
  onTimeClick?: (panelId: string, time: number | null) => void
  /** Draws hedgeFund regime labels under the candles (from the lab, closed bars only). */
  regimeEnabled: boolean
  regimeClient: LabRegimeClient
  /**
   * Draws historical funding and open-interest panes from the lab's futures archive (closed bars
   * only; no live stream).
   */
  derivativesEnabled?: boolean
  /** Required for the panes to load; without it they stay off. */
  derivativesClient?: LabDerivativesClient
  /** Why the panes are off on every chart of this venue (no requests), or null. */
  derivativesBlocked?: string | null
  /** Replaces the "Loading older bars…" hint while set (e.g. the backtest history chip). */
  statusChip?: ReactNode
  /** The bar replay controls, on the replay panel only (a stable element). */
  replayBar?: ReactNode
  /** Extra overlays inside the chart area (e.g. the drawing style bar). */
  children?: ReactNode
}

/** Candles while the venue is unknown: never loads, never errors (aborts quietly). */
const VENUE_PENDING_SOURCE: CandleSource = {
  getCandles: (_symbol, _tf, _limit, _endTime, opts) =>
    new Promise<never>((_resolve, reject) => {
      opts?.signal?.addEventListener('abort', () =>
        reject(new MarketDataError('aborted', 'Request aborted')),
      )
    }),
  subscribeCandles: () => () => {},
}

/** The regime chip's text on charts the ribbon does not support. */
export const REGIME_SECONDS_TEXT = 'Regime: not on seconds timeframes'

export const ChartPanel = memo(function ChartPanel({
  panel,
  provider,
  venuePending = false,
  limit,
  errorSource,
  source,
  exchange,
  active,
  multi,
  onActivate,
  onLive,
  onApi,
  appearance,
  indicators,
  onIndicatorToggleVisible,
  onIndicatorRemove,
  onIndicatorSettings,
  drawings,
  tool,
  onToolDone,
  drawingStyle,
  selectedDrawingId,
  onSelectDrawing,
  onContextMenu,
  alerts,
  backtest,
  focusRange,
  onFocusDone,
  keyboardBlocked,
  crosshairSync,
  candlesOverride = null,
  replaySession = false,
  onTimeClick,
  regimeEnabled,
  regimeClient,
  derivativesEnabled = false,
  derivativesClient = NO_DERIVATIVES_CLIENT,
  derivativesBlocked = null,
  statusChip,
  replayBar,
  children,
}: ChartPanelProps) {
  const { id, symbol, interval, chartType } = panel
  const live = useLiveCandles(
    venuePending ? VENUE_PENDING_SOURCE : provider,
    symbol,
    interval,
    limit,
    errorSource,
    MAX_HISTORY_BARS,
  )
  const { load } = live

  useEffect(() => {
    onLive?.(id, live, { symbol, interval })
  }, [onLive, id, live, symbol, interval])
  useEffect(() => {
    if (!onLive) return
    return () => onLive(id, null)
  }, [onLive, id])

  const alertLines = useMemo(() => alertLinesFor(alerts, symbol), [alerts, symbol])
  const chartApi = useCallback((api: ChartApi | null) => onApi?.(id, api), [onApi, id])
  const selectDrawing = useCallback(
    (drawingId: string | null) => onSelectDrawing(id, drawingId),
    [onSelectDrawing, id],
  )
  const contextMenu = useCallback(
    (request: ChartContextMenuRequest) => onContextMenu(id, request),
    [onContextMenu, id],
  )
  const activate = useCallback(() => onActivate(id), [onActivate, id])
  const timeClick = useMemo(
    () => (onTimeClick ? (time: number | null) => onTimeClick(id, time) : undefined),
    [onTimeClick, id],
  )

  const candles = load.kind === 'ready' ? load.candles : null
  const firstBarTime = candles !== null && candles.length > 0 ? candles[0].time : null
  const replaying = candlesOverride !== null
  // What the chart plots: the replay's bars, else the live bars without the no-trade ones.
  const plotted = candlesOverride ?? (load.kind === 'ready' ? load.bars : NO_BARS)
  const hasBars = load.kind === 'ready' && plotted.length > 0

  // Labels for every loaded closed bar, no-trade bars included (the lab feature's rows): in
  // replay the bars up to the cursor, live all but the forming last one. The chart draws them
  // on plotted bars only. Seconds charts get none (a bar closes every second).
  const regimeAllowed = regimeSupported(interval)
  const regimeSource = useMemo(
    () => regimeSourceBars(candles ?? NO_BARS, candlesOverride),
    [candles, candlesOverride],
  )
  const regime = useRegime(regimeClient, {
    enabled: regimeEnabled && regimeAllowed && hasBars,
    symbol,
    timeframe: interval,
    candles: regimeSource.candles,
    formingLast: regimeSource.formingLast,
    windowBars: REGIME_WINDOW_BARS,
    threshold: REGIME_THRESHOLD,
    barSeconds: timeframeSeconds(interval),
  })

  // Funding / OI for the same closed bars as the ribbon (live: all but the forming bar; replay:
  // up to the cursor). Off on seconds charts, bars over a week, non-Binance symbols and venues.
  const derivativesOff = derivativesBlocked ?? derivativesUnsupportedReason(interval, symbol)
  const derivatives = useDerivatives(derivativesClient, {
    enabled:
      derivativesEnabled &&
      derivativesClient !== NO_DERIVATIVES_CLIENT &&
      derivativesOff === null &&
      hasBars,
    symbol,
    timeframe: interval,
    barSeconds: timeframeSeconds(interval),
    candles: regimeSource.candles,
    formingLast: regimeSource.formingLast,
  })

  const cellClass = `chart-cell${multi ? ' chart-cell--multi' : ''}${
    multi && active ? ' chart-cell--active' : ''
  }${replaySession ? ' chart-cell--replay' : ''}`

  return (
    // Capture phase: the panel is active before the chart sees the press (drawing tools,
    // selection, context menu all act on the panel under the pointer).
    <section
      className={cellClass}
      onPointerDownCapture={activate}
      onFocusCapture={activate}
      aria-label={multi ? `${symbol} ${interval} chart${active ? ' (active)' : ''}` : undefined}
    >
      {multi && (
        <div className="chart-cell__strip">
          <span className="chart-cell__symbol">{symbol}</span>
          <span className="chart-cell__interval">{interval}</span>
          <ConnectionIndicator status={live.status} variant="compact" />
          {replaySession && <span className="chart-cell__replay-tag">Replay</span>}
          {active && <span className="chart-cell__badge">Active</span>}
        </div>
      )}
      <div className="chart-cell__body">
        {hasBars && (
          <Chart
            candles={plotted}
            historyStart={firstBarTime}
            symbol={symbol}
            interval={interval}
            source={source}
            chartType={chartType}
            onNearLeftEdge={live.loadOlder}
            indicators={indicators}
            onIndicatorToggleVisible={onIndicatorToggleVisible}
            onIndicatorRemove={onIndicatorRemove}
            onIndicatorSettings={onIndicatorSettings}
            drawings={drawings.items}
            tool={tool}
            selectedDrawingId={selectedDrawingId}
            onSelectDrawing={selectDrawing}
            onDrawingCreate={drawings.add}
            drawingStyle={drawingStyle}
            onToolDone={onToolDone}
            onDrawingChange={drawings.update}
            onDrawingDelete={drawings.remove}
            onContextMenu={contextMenu}
            alertLines={alertLines}
            backtest={backtest}
            focusRange={focusRange}
            onFocusDone={onFocusDone}
            appearance={appearance}
            onApi={chartApi}
            // Escape / Delete belong to the active panel only.
            keyboardBlocked={keyboardBlocked || !active}
            crosshairSync={crosshairSync}
            crosshairSyncId={id}
            regime={regimeEnabled && regimeAllowed ? regime.points : null}
            derivatives={derivativesEnabled && derivativesOff === null ? derivatives.overlay : null}
            onTimeClick={timeClick}
            replayActive={replaying}
          />
        )}
        {load.kind === 'ready' && plotted.length === 0 && (
          <NoTradesView
            symbol={symbol}
            interval={interval}
            exchange={exchange}
            raw={load.candles}
            maxBars={MAX_HISTORY_BARS}
            loadingOlder={live.loadingOlder}
            historyExhausted={live.historyExhausted}
            atLimit={atHistoryLimit(load.candles.length, MAX_HISTORY_BARS)}
            retryBlocked={live.retryBlocked}
            olderCooldown={live.olderCooldown}
            offline={live.status === 'offline'}
            onLoadOlder={live.loadOlder}
          />
        )}
        {hasBars && regimeEnabled && (
          <RegimeChip overlay={regime} unavailable={regimeAllowed ? null : REGIME_SECONDS_TEXT} />
        )}
        {hasBars && derivativesEnabled && (
          <DerivativesChip
            state={derivatives}
            unavailable={derivativesOff}
            stacked={regimeEnabled}
          />
        )}
        {hasBars && replayBar}
        {hasBars && children}
        {statusChip ??
          (hasBars && live.loadingOlder && (
            <div className="history-hint" role="status">
              <span className="history-hint__spinner" aria-hidden="true" />
              Loading older bars…
            </div>
          ))}
        {load.kind === 'loading' && (
          <LoadingView
            label={
              venuePending
                ? `Loading ${symbol} ${interval}: waiting for the local server to report its exchange…`
                : `Loading ${symbol} ${interval} from ${source}…`
            }
          />
        )}
        {load.kind === 'error' && (
          <ErrorView
            title={load.title}
            message={load.message}
            onRetry={live.reload}
            retryDisabled={live.retryBlocked}
          />
        )}
      </div>
    </section>
  )
})

const NO_BARS: readonly Candle[] = []

/** Stand-in when no client is passed: the panes are off, so it is never called. */
const NO_DERIVATIVES_CLIENT: LabDerivativesClient = {
  fetchDerivatives: () => Promise.reject(new Error('No lab client')),
}

const REGIME_LAB_COMMAND = 'uv run python -m lab.api'

/**
 * Top-right chip naming the ribbon's parameters, with a legend in its tooltip. If the lab is
 * unreachable it says how to start it; the chart keeps working either way.
 */
function RegimeChip({
  overlay,
  unavailable,
}: {
  overlay: RegimeOverlay
  /** Why the ribbon is off on this chart (no requests are made), or null. */
  unavailable: string | null
}) {
  if (unavailable !== null) {
    return (
      <div
        className="regime-chip regime-chip--off"
        title="The regime ribbon is off on seconds charts: a bar closes every second. Pick 1m or longer to see it."
      >
        <span className="regime-chip__label">{unavailable}</span>
      </div>
    )
  }
  const label = regimeParamsLabel(REGIME_WINDOW_BARS, REGIME_THRESHOLD)
  const { error, loading, points } = overlay
  const offline = error === LAB_OFFLINE_MESSAGE
  const title =
    error !== null
      ? offline
        ? `Regime needs the lab: run \`${REGIME_LAB_COMMAND}\` in lab/ (or \`npm run dev\` at the repo root).`
        : `Regime labels failed: ${error}`
      : `hedgeFund regime of each closed bar from its trailing ${REGIME_WINDOW_BARS}-bar return ` +
        `(±${REGIME_THRESHOLD * 100}% bands): bull, sideways, bear. Each label uses only that ` +
        'bar and earlier ones, so it never changes later. The forming bar has none.'
  return (
    <div
      className={`regime-chip${error !== null ? ' regime-chip--error' : ''}`}
      title={title}
      role={error !== null ? 'status' : undefined}
    >
      <span className="regime-chip__swatches" aria-hidden="true">
        <span className="regime-chip__swatch regime-chip__swatch--bull" />
        <span className="regime-chip__swatch regime-chip__swatch--side" />
        <span className="regime-chip__swatch regime-chip__swatch--bear" />
      </span>
      <span className="regime-chip__label">{label}</span>
      {error !== null ? (
        <span className="regime-chip__state">
          {offline ? (
            <>
              needs the lab: <code>{REGIME_LAB_COMMAND}</code> in lab/
            </>
          ) : (
            'unavailable'
          )}
        </span>
      ) : (
        loading &&
        points === null && (
          <span className="history-hint__spinner regime-chip__spinner" aria-hidden="true" />
        )
      )}
    </div>
  )
}

/** UTC date of a UNIX-seconds time, e.g. "2026-10-25". */
function utcDate(seconds: number): string {
  return new Date(seconds * 1000).toISOString().slice(0, 10)
}

/**
 * Small chip under the regime chip (or in its place) for the funding / OI panes: the archive's
 * end date when loaded, the CLI that downloads the archive when the lab has none for the
 * symbol, how to start the lab when it is offline, or why the panes are off on this chart.
 */
function DerivativesChip({
  state,
  unavailable,
  stacked,
}: {
  state: DerivativesState
  /** Why the panes are off on this chart (no requests are made), or null. */
  unavailable: string | null
  /** The regime chip is shown above. */
  stacked: boolean
}) {
  const base = `regime-chip deriv-chip${stacked ? ' deriv-chip--stacked' : ''}`
  if (unavailable !== null) {
    return (
      <div
        className={`${base} regime-chip--off`}
        title="Funding and open interest come from Binance's USDⓈ-M futures archive, aligned to bars of 1 minute to 1 week on Binance spot charts."
      >
        <span className="regime-chip__label">{unavailable}</span>
      </div>
    )
  }
  const { error, errorKind, loading, overlay, available } = state
  const end = derivativesArchiveEnd(available)
  const title =
    error !== null
      ? errorKind === 'offline'
        ? `Funding / OI needs the lab: run \`${REGIME_LAB_COMMAND}\` in lab/ (or \`npm run dev\` at the repo root).`
        : error
      : "Historical funding rate (%, green ≥ 0, red < 0) and open interest (USD) from the lab's " +
        "copy of Binance's USDⓈ-M archive. Each bar shows what was published by its close; blank " +
        'bars have no data (a gap or past the downloaded range). No live futures stream.' +
        (end !== null ? ` Archive ends ${utcDate(end)} (UTC).` : '') +
        ' After downloading more of the archive, switch Funding / OI off and on to reload.'
  const hint = errorKind === 'no-data' && error !== null ? quotedCommand(error) : null
  return (
    <div
      className={`${base}${error !== null ? ' regime-chip--error' : ''}`}
      title={title}
      role={error !== null ? 'status' : undefined}
    >
      <span className="deriv-chip__icon" aria-hidden="true">
        <span className="deriv-chip__bar deriv-chip__bar--pos" />
        <span className="deriv-chip__bar deriv-chip__bar--neg" />
        <span className="deriv-chip__line" />
      </span>
      <span className="regime-chip__label">Funding / OI</span>
      {error !== null ? (
        <span className="regime-chip__state">
          {errorKind === 'offline' ? (
            <>
              needs the lab: <code>{REGIME_LAB_COMMAND}</code>
            </>
          ) : errorKind === 'no-data' ? (
            hint !== null ? (
              <>
                no archive: <code>{hint}</code>
              </>
            ) : (
              'no archive downloaded'
            )
          ) : (
            'unavailable'
          )}
        </span>
      ) : loading && overlay === null ? (
        <span className="history-hint__spinner regime-chip__spinner" aria-hidden="true" />
      ) : (
        end !== null && <span className="deriv-chip__meta">to {utcDate(end)}</span>
      )}
    </div>
  )
}
