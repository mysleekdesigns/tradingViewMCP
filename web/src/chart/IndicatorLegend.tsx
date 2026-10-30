import { memo } from 'react'
import type { IndicatorLegendRow } from './indicatorSeries'
import './indicators.css'

export interface IndicatorLegendActions {
  onToggleVisible?: (uid: string) => void
  onRemove?: (uid: string) => void
  onSettings?: (uid: string) => void
}

interface HighlightProps {
  /**
   * uid of the indicator under the mouse on the chart (LWC hit testing); its row is highlighted
   * the way a hovered row looks, as TradingView does.
   */
  highlightedUid?: string | null
}

interface RowsProps extends IndicatorLegendActions, HighlightProps {
  rows: readonly IndicatorLegendRow[]
}

/**
 * Overlay and volume indicator rows; rendered inside the main (top-left) legend. Memo'd: the rows
 * array is stable per indicator state (`partitionLegendRows`) and the actions are stable
 * callbacks, so a chart render that changes neither skips this.
 */
export const IndicatorLegendRows = memo(function IndicatorLegendRows({
  rows,
  highlightedUid = null,
  ...actions
}: RowsProps) {
  return (
    <>
      {rows.map((row) => (
        <IndicatorLegendItem
          key={row.uid}
          row={row}
          highlighted={row.uid === highlightedUid}
          {...actions}
        />
      ))}
    </>
  )
})

interface PaneLegendsProps extends IndicatorLegendActions, HighlightProps {
  /** Pane-indicator rows (row.pane >= 1). */
  rows: readonly IndicatorLegendRow[]
  /** Top of each pane in px, relative to the chart container; index = pane index. */
  paneTops: readonly (number | null)[]
}

/**
 * One legend at the top-left of each indicator pane. Hidden until the pane has been measured.
 * Memo'd like `IndicatorLegendRows` (`paneTops` keeps its identity while the tops are unchanged).
 */
export const PaneLegends = memo(function PaneLegends({
  rows,
  paneTops,
  highlightedUid = null,
  ...actions
}: PaneLegendsProps) {
  return (
    <>
      {rows.map((row) => {
        const top = paneTops[row.pane]
        if (top === null || top === undefined) return null
        return (
          <div key={row.uid} className="ind-pane-legend" style={{ top: top + 6 }} aria-live="off">
            <IndicatorLegendItem row={row} highlighted={row.uid === highlightedUid} {...actions} />
          </div>
        )
      })}
    </>
  )
})

interface ItemProps extends IndicatorLegendActions {
  row: IndicatorLegendRow
  highlighted: boolean
}

/**
 * Row class names: base, hidden, highlighted (indicator hovered on the chart), and with a note
 * (one line, no wrap).
 */
function legendRowClassName(visible: boolean, highlighted: boolean, noted: boolean): string {
  let name = 'ind-legend__row'
  if (!visible) name += ' ind-legend__row--hidden'
  if (highlighted) name += ' ind-legend__row--highlighted'
  if (noted) name += ' ind-legend__row--note'
  return name
}

function IndicatorLegendItem({
  row,
  highlighted,
  onToggleVisible,
  onRemove,
  onSettings,
}: ItemProps) {
  const { uid, title, visible, values, note, hint } = row
  const showNote = visible && note !== undefined
  return (
    <div
      className={legendRowClassName(visible, highlighted, showNote)}
      data-highlighted={highlighted ? 'true' : undefined}
      title={hint}
    >
      <span className="ind-legend__title">{title}</span>
      {visible && (
        <span className="ind-legend__values">
          {values.map((v) => (
            <span key={v.key} className="ind-legend__value" style={{ color: v.color }}>
              {v.text}
            </span>
          ))}
        </span>
      )}
      {showNote && (
        <span className="ind-legend__note" role="note" title={note}>
          {note}
        </span>
      )}
      <span className="ind-legend__actions">
        <button
          type="button"
          className="ind-legend__btn ind-legend__btn--eye"
          aria-label={`Hide ${title}`}
          aria-pressed={!visible}
          title={visible ? 'Hide' : 'Show'}
          onClick={() => onToggleVisible?.(uid)}
        >
          {visible ? <EyeIcon /> : <EyeOffIcon />}
        </button>
        <button
          type="button"
          className="ind-legend__btn"
          aria-label={`Settings for ${title}`}
          title="Settings"
          onClick={() => onSettings?.(uid)}
        >
          <GearIcon />
        </button>
        <button
          type="button"
          className="ind-legend__btn ind-legend__btn--remove"
          aria-label={`Remove ${title}`}
          title="Remove"
          onClick={() => onRemove?.(uid)}
        >
          <CloseIcon />
        </button>
      </span>
    </div>
  )
}

const iconProps = {
  width: 14,
  height: 14,
  viewBox: '0 0 18 18',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
  focusable: false,
} as const

function EyeIcon() {
  return (
    <svg {...iconProps}>
      <path d="M1.5 9S4.2 3.8 9 3.8 16.5 9 16.5 9 13.8 14.2 9 14.2 1.5 9 1.5 9Z" />
      <circle cx="9" cy="9" r="2.4" />
    </svg>
  )
}

function EyeOffIcon() {
  return (
    <svg {...iconProps}>
      <path d="M7.1 4.05A7 7 0 0 1 9 3.8C13.8 3.8 16.5 9 16.5 9a13 13 0 0 1-2 2.7M11 13.9a7 7 0 0 1-2 .3C4.2 14.2 1.5 9 1.5 9a13 13 0 0 1 2.9-3.5" />
      <path d="M7.4 7.4a2.4 2.4 0 0 0 3.2 3.2" />
      <path d="M2.5 2.5l13 13" />
    </svg>
  )
}

function GearIcon() {
  return (
    <svg {...iconProps}>
      <circle cx="9" cy="9" r="2.3" />
      <path d="M9 1.8v1.9M9 14.3v1.9M1.8 9h1.9M14.3 9h1.9M3.9 3.9l1.35 1.35M12.75 12.75l1.35 1.35M3.9 14.1l1.35-1.35M12.75 5.25l1.35-1.35" />
      <circle cx="9" cy="9" r="5" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg {...iconProps}>
      <path d="M4.5 4.5l9 9M13.5 4.5l-9 9" />
    </svg>
  )
}
