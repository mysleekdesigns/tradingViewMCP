/*
 * Icons shared by the always-loaded shell (the right rail) and the lazily loaded panels. They
 * live here, not in BacktestPanel / ResearchPage, so the rail can draw them without pulling
 * those panels into the main chunk.
 */

const STROKE = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

/** Rising equity line with an arrow: the Strategy Tester (dock title and the rail button). */
export function TesterIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 22 22" {...STROKE}>
      <path d="M3.5 17.5h15M5 14.5l3.5-4 3 2.5 5-6.5" />
      <path d="M14 6.5h2.5V9" />
    </svg>
  )
}

/** Flask with a rising line: research (the right rail's Research button uses it too). */
export function ResearchIcon({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 22 22" {...STROKE}>
      <path d="M8.5 3h5M9.5 3v5.2L4.6 16.6A1.6 1.6 0 0 0 6 19h10a1.6 1.6 0 0 0 1.4-2.4L12.5 8.2V3" />
      <path d="M6.8 14.5 9.5 12l2 1.6 3.6-3.1" />
    </svg>
  )
}
