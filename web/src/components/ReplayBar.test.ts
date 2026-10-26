import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { REPLAY_OFF } from '../state/replay'
import type { UseReplay } from '../state/useReplay'
import { REPICK_HINT, REPLAY_SCOPE_NOTE, ReplayBar } from './ReplayBar'

const noop = () => {}

function replay(status: 'off' | 'active' | 'picking', cursorTime: number | null = null): UseReplay {
  return {
    state: { ...REPLAY_OFF, status, cursorTime: status === 'active' ? 120 : cursorTime },
    visible: status === 'active' ? [] : null,
    currentBar: null,
    atEnd: false,
    barsAhead: 3,
    revision: 0,
    startPicking: noop,
    cancelPicking: noop,
    pickAt: noop,
    startBarsBack: noop,
    stepForward: noop,
    stepBack: noop,
    play: noop,
    pause: noop,
    togglePlay: noop,
    setSpeed: noop,
    exit: noop,
  }
}

describe('ReplayBar', () => {
  it('says replay is a look-ahead check for this chart only', () => {
    expect(REPLAY_SCOPE_NOTE).toMatch(/this chart only/)
    expect(REPLAY_SCOPE_NOTE).toMatch(/Other panels, the watchlist, drawings, alert lines/)
    expect(REPLAY_SCOPE_NOTE).toMatch(
      /Seasonals, the Strategy Tester trade list and the alert dialog/,
    )
    expect(REPLAY_SCOPE_NOTE).toMatch(/prefilled with the live last close/)
    for (const status of ['off', 'active'] as const) {
      const html = renderToStaticMarkup(createElement(ReplayBar, { replay: replay(status) }))
      expect(html).toContain('class="replay-bar__info"')
      expect(html).toContain('still show live (future) information')
    }
  })

  it('tells a re-pick it can only go back, and a fresh pick that any bar works', () => {
    const repick = renderToStaticMarkup(
      createElement(ReplayBar, { replay: replay('picking', 120) }),
    )
    expect(repick).toContain('Click an earlier bar to restart from it (this bar resumes)')
    expect(REPICK_HINT).toMatch(/To start later, exit and start a new replay/)
    const fresh = renderToStaticMarkup(createElement(ReplayBar, { replay: replay('picking') }))
    expect(fresh).toContain('Click a bar on the chart to replay from it')
  })
})
