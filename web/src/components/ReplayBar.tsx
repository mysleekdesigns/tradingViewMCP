import type { ReactNode } from 'react'
import { formatDateTime, timezoneLabel } from '../chart/timeFormat'
import { isRepicking, REPLAY_SPEEDS } from '../state/replay'
import { isMacPlatform } from '../state/shortcuts'
import type { UseReplay } from '../state/useReplay'
import './Replay.css'

export interface ReplayBarProps {
  replay: UseReplay
  /** Display zone for the bar's date (a TimezoneSetting). Default 'UTC'. */
  timeZone?: string
  /** Hide the bar. Exit calls replay.exit() first, so the live view is back by then. */
  onClose?: () => void
}

/** Bars replay starts back from when the user does not pick a start bar. */
export const DEFAULT_BARS_BACK = 100

/** Bars the "skip" button jumps forward. */
export const REPLAY_SKIP_BARS = 10

/**
 * Pick-mode hint for a re-pick from a running replay: the chart keeps the replay view, so only
 * the current bar (resumes) or an earlier one (restarts) can be picked.
 */
export const REPICK_HINT =
  'Click an earlier bar to restart from it with a fresh paper account; clicking this bar (or ' +
  'cancelling) resumes the replay as it was. To start later, exit and start a new replay.'

/** What replay hides and what it does not (the bar's info tooltip). */
export const REPLAY_SCOPE_NOTE =
  'Replay hides future bars on this chart only: its candles and indicators. Other panels, the ' +
  'watchlist, drawings, alert lines, Seasonals, the Strategy Tester trade list and the alert ' +
  'dialog (prefilled with the live last close) still show live (future) information, so replay ' +
  'is a look-ahead check for this chart’s candles and indicators only.'

const ICON = {
  width: 18,
  height: 18,
  viewBox: '0 0 18 18',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

const PickIcon = () => (
  <svg {...ICON}>
    <path d="M9 2.5v3M9 12.5v3M2.5 9h3M12.5 9h3" />
    <circle cx="9" cy="9" r="3.5" />
    <circle cx="9" cy="9" r="0.6" fill="currentColor" />
  </svg>
)

const StepBackIcon = () => (
  <svg {...ICON}>
    <path d="M4.5 4v10" />
    <path d="M13.5 4.5 7 9l6.5 4.5z" fill="currentColor" />
  </svg>
)

const StepForwardIcon = () => (
  <svg {...ICON}>
    <path d="M13.5 4v10" />
    <path d="M4.5 4.5 11 9l-6.5 4.5z" fill="currentColor" />
  </svg>
)

const SkipIcon = () => (
  <svg {...ICON}>
    <path d="M2.5 4.5 8 9l-5.5 4.5z" fill="currentColor" />
    <path d="M8.5 4.5 14 9l-5.5 4.5z" fill="currentColor" />
    <path d="M15.5 4v10" />
  </svg>
)

const PlayIcon = () => (
  <svg {...ICON}>
    <path d="M5.5 3.5 14.5 9l-9 5.5z" fill="currentColor" />
  </svg>
)

const PauseIcon = () => (
  <svg {...ICON}>
    <path d="M5 3.5v11M13 3.5v11" strokeWidth={3} strokeLinecap="butt" />
  </svg>
)

const InfoIcon = () => (
  <svg {...ICON} width={14} height={14}>
    <circle cx="9" cy="9" r="6.5" />
    <path d="M9 8.2v4.3" />
    <circle cx="9" cy="5.6" r="0.5" fill="currentColor" />
  </svg>
)

const CloseIcon = () => (
  <svg {...ICON}>
    <path d="M4.5 4.5l9 9M13.5 4.5l-9 9" />
  </svg>
)

function BarButton(props: {
  label: string
  shortcut?: string
  onClick: () => void
  disabled?: boolean
  pressed?: boolean
  variant?: 'primary'
  children: ReactNode
}) {
  const { label, shortcut, onClick, disabled, pressed, variant, children } = props
  return (
    <button
      type="button"
      className={`replay-bar__btn${variant === 'primary' ? ' replay-bar__btn--primary' : ''}`}
      aria-label={label}
      aria-pressed={pressed}
      title={shortcut ? `${label} (${shortcut})` : label}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

/**
 * Floating bar replay controls. Renders absolutely at the bottom centre of its positioned parent
 * (the chart area), above the time axis. The amber "Replay" badge and frame stay on screen
 * whenever replay is not live, so replayed history is never mistaken for live data.
 */
export function ReplayBar({ replay, timeZone = 'UTC', onClose }: ReplayBarProps) {
  const { state, currentBar, atEnd, barsAhead, visible } = replay
  const shift = isMacPlatform() ? '⇧' : 'Shift+'
  const playing = state.status === 'active' && state.playing

  const exit = () => {
    replay.exit()
    onClose?.()
  }

  const shown = visible?.length ?? 0
  const progress = shown + barsAhead > 0 ? shown / (shown + barsAhead) : 0

  return (
    <div
      className={`replay-bar replay-bar--${state.status}${playing ? ' replay-bar--playing' : ''}`}
      role="toolbar"
      aria-label="Bar replay"
      onContextMenu={(e) => e.preventDefault()}
    >
      <span
        className="replay-bar__badge"
        title="Bar replay: the chart shows history, not live data"
      >
        <span className="replay-bar__dot" aria-hidden="true" />
        Replay
      </span>
      <span
        className="replay-bar__info"
        role="img"
        tabIndex={0}
        aria-label={REPLAY_SCOPE_NOTE}
        title={REPLAY_SCOPE_NOTE}
      >
        <InfoIcon />
      </span>

      {state.status === 'active' ? (
        <>
          <BarButton label="Pick a new start bar" onClick={replay.startPicking}>
            <PickIcon />
          </BarButton>
          <span className="replay-bar__sep" aria-hidden="true" />
          <BarButton
            label="Step back"
            shortcut={`${shift}←`}
            onClick={() => replay.stepBack()}
            disabled={shown <= 1}
          >
            <StepBackIcon />
          </BarButton>
          <BarButton
            label={playing ? 'Pause' : 'Play'}
            shortcut={`${shift}↓`}
            onClick={replay.togglePlay}
            disabled={!playing && atEnd}
            variant="primary"
          >
            {playing ? <PauseIcon /> : <PlayIcon />}
          </BarButton>
          <BarButton
            label="Step forward"
            shortcut={`${shift}→`}
            onClick={() => replay.stepForward()}
            disabled={atEnd}
          >
            <StepForwardIcon />
          </BarButton>
          <BarButton
            label={`Skip ${REPLAY_SKIP_BARS} bars forward`}
            onClick={() => replay.stepForward(REPLAY_SKIP_BARS)}
            disabled={atEnd}
          >
            <SkipIcon />
          </BarButton>
          <span className="replay-bar__sep" aria-hidden="true" />
          <div className="replay-bar__speeds" role="group" aria-label="Playback speed">
            {REPLAY_SPEEDS.map((s) => (
              <button
                key={s}
                type="button"
                className="replay-bar__speed"
                aria-pressed={state.speed === s}
                aria-label={`${s} ${s === 1 ? 'bar' : 'bars'} per second`}
                title={`${s} ${s === 1 ? 'bar' : 'bars'} per second`}
                onClick={() => replay.setSpeed(s)}
              >
                {s}×
              </button>
            ))}
          </div>
          <span className="replay-bar__sep" aria-hidden="true" />
          <span className="replay-bar__readout" aria-live="polite">
            <span className="replay-bar__time">
              {currentBar ? formatDateTime(currentBar.time, timeZone) : '—'}
              {currentBar && (
                <span className="replay-bar__zone">
                  {' '}
                  {timezoneLabel(timeZone, currentBar.time)}
                </span>
              )}
            </span>
            <span className="replay-bar__ahead">
              {atEnd ? 'Latest closed bar' : `${barsAhead.toLocaleString()} bars ahead`}
            </span>
          </span>
        </>
      ) : state.status === 'picking' ? (
        <>
          <span
            className="replay-bar__hint"
            role="status"
            title={isRepicking(state) ? REPICK_HINT : undefined}
          >
            <PickIcon />
            <span className="replay-bar__hint-text">
              {isRepicking(state)
                ? 'Click an earlier bar to restart from it (this bar resumes)'
                : 'Click a bar on the chart to replay from it'}
            </span>
          </span>
          <button
            type="button"
            className="replay-bar__text-btn"
            title="Cancel (Esc)"
            onClick={replay.cancelPicking}
          >
            Cancel
          </button>
        </>
      ) : (
        <>
          <button
            type="button"
            className="replay-bar__text-btn replay-bar__text-btn--accent"
            title="Choose the bar replay starts from"
            onClick={replay.startPicking}
          >
            <PickIcon />
            Pick start
          </button>
          <button
            type="button"
            className="replay-bar__text-btn"
            title={`Start ${DEFAULT_BARS_BACK} bars before the latest closed bar`}
            onClick={() => replay.startBarsBack(DEFAULT_BARS_BACK)}
          >
            Last {DEFAULT_BARS_BACK} bars
          </button>
        </>
      )}

      <span className="replay-bar__sep" aria-hidden="true" />
      <button
        type="button"
        className="replay-bar__text-btn replay-bar__exit"
        aria-label="Exit replay"
        title={state.status === 'off' ? 'Close replay' : 'Exit replay and return to live (Esc)'}
        onClick={exit}
      >
        <CloseIcon />
        <span className="replay-bar__exit-label">Exit</span>
      </button>

      {state.status === 'active' && (
        <span
          className="replay-bar__progress"
          aria-hidden="true"
          style={{ transform: `scaleX(${progress})` }}
        />
      )}
    </div>
  )
}
