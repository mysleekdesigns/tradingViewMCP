import {
  memo,
  type ErrorInfo,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { LEDGER_PAGE_SIZE, type LabClient } from '../data/labApi'
import type { LedgerItem } from '../data/labTypes'
import { IDEA_STATUSES, type ResearchClient } from '../data/researchApi'
import type {
  IdeaStatus,
  JournalEntry,
  PaperComparison,
  ResearchCandidate,
  ResearchIdea,
  RunItem,
  TradeStats,
} from '../data/researchTypes'
import { ErrorBoundary } from './ErrorBoundary'
import { EMPTY, fmtInt, fmtPct, fmtRatio, fmtSignedPct, pageInfo, pageOffset } from './backtestView'
import {
  CANDIDATE_EXPLAINER,
  RESEARCH_TABS,
  budgetMeters,
  candidateRunRequest,
  criteriaDefinitions,
  criteriaRows,
  describeResearchError,
  fmtDate,
  fmtDateTime,
  fmtUtc,
  ideaFamilies,
  ideaStatusView,
  isStale,
  journalNameFromPath,
  liveChipState,
  loadResearchTab,
  outcomeView,
  paperFor,
  parseInline,
  parseMarkdown,
  phaseSteps,
  pickJournalEntry,
  recentEvents,
  relativeTime,
  safeHttpUrl,
  hostOf,
  runsPollDelay,
  runsRefreshNote,
  fmtClockUtc,
  resetResearchTab,
  journalTargetAfterTabChange,
  saveResearchTab,
  shortHash,
  summaryView,
  toEpochMs,
  unmatchedPaper,
  verdictView,
  type CandidateRunRequest,
  type Inline,
  type LiveChipState,
  type MdBlock,
  type ResearchErrorView,
  type ResearchTab,
  type ResearchTone,
  type RunsPollTrigger,
} from './researchView'
import { ResearchIcon } from './icons'
import { ChartIcon, WarnIcon } from './ResearchFallback'
import { useEscapeToClose } from './useEscapeToClose'
import './Research.css'

// Research's render-error panel is in the main chunk now (ResearchFallback.tsx); re-exported
// here for existing importers.
export { ResearchFallback, type ResearchFallbackProps } from './ResearchFallback'

function pageHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden
}

interface RunsFeed {
  runs: RunItem[] | null
  error: ResearchErrorView | null
  fetchedAt: number | null
  /** Ask again now (ignored while a request is in flight). */
  refresh(): void
}

/**
 * The research run list: loaded at once, then re-asked `runsPollDelay` after each answer settles
 * (one request at a time, never an overlapping or aborted poll): every 5 s while a run is live,
 * every 30 s while nothing runs (so a new run shows up), every 8 s while the lab is offline, and
 * not at all while the only running run looks stalled or the tab is hidden. When the tab becomes
 * visible again it always asks once, and that fresh answer decides whether polling continues.
 * Unmounting cancels the timer and the request in flight.
 */
function useResearchRuns(client: ResearchClient): RunsFeed {
  const [runs, setRuns] = useState<RunItem[] | null>(null)
  const [error, setError] = useState<ResearchErrorView | null>(null)
  const [fetchedAt, setFetchedAt] = useState<number | null>(null)
  const refreshRef = useRef<() => void>(() => {})

  useEffect(() => {
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let inflight: AbortController | null = null
    let lastRuns: RunItem[] | null = null
    let offline = false

    const clearTimer = (): void => {
      if (timer !== undefined) clearTimeout(timer)
      timer = undefined
    }

    /** Load now or set the next timer, as `runsPollDelay` decides for this trigger. */
    const schedule = (trigger: RunsPollTrigger): void => {
      if (disposed || inflight !== null) return
      const ms = runsPollDelay({
        runs: lastRuns,
        offline,
        hidden: pageHidden(),
        nowMs: Date.now(),
        trigger,
      })
      if (ms === null) return
      if (ms === 0) {
        load()
        return
      }
      if (timer !== undefined) return
      timer = setTimeout(() => {
        timer = undefined
        load()
      }, ms)
    }

    function load(): void {
      if (disposed || inflight !== null) return
      clearTimer()
      const controller = new AbortController()
      inflight = controller
      client
        .runs(controller.signal)
        .then(
          (items) => {
            if (disposed) return
            lastRuns = items
            offline = false
            setRuns(items)
            setError(null)
            setFetchedAt(Date.now())
          },
          (err: unknown) => {
            if (disposed) return
            const view = describeResearchError(err)
            if (view.kind === 'aborted') return
            offline = view.kind === 'offline'
            setError(view)
          },
        )
        .finally(() => {
          if (inflight === controller) inflight = null
          schedule('response')
        })
    }

    const onVisibility = (): void => {
      if (pageHidden()) {
        clearTimer()
        return
      }
      // Back in view: ask once whatever the old list said (a request in flight reschedules
      // itself when it settles).
      schedule('visible')
    }

    refreshRef.current = load
    schedule('initial')
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      disposed = true
      clearTimer()
      inflight?.abort()
      document.removeEventListener('visibilitychange', onVisibility)
      refreshRef.current = () => {}
    }
  }, [client])

  return { runs, error, fetchedAt, refresh: () => refreshRef.current() }
}

export interface ResearchPageProps {
  client: ResearchClient
  labClient: LabClient
  /** Back to the chart. */
  onClose(): void
  /** Load the candidate's plan into the Strategy Tester, switch the chart to it, and run it. */
  onShowCandidate(request: CandidateRunRequest): void
  /** Load the plan into the Strategy Tester without running it. */
  onLoadPlan(request: CandidateRunRequest): void
  /** A Strategy Tester backtest is running; a candidate run waits for it. */
  testerBusy: boolean
}

/**
 * Research view (Phase 8): live research-run progress, the idea library, the trial ledger, the
 * research journal, and candidates with their paper-trading comparisons. It covers the chart
 * area while open; the chart stays mounted underneath, so closing it (Esc, "Back to chart", or a
 * candidate click) shows the chart again without reloading. Read-only: nothing here starts runs,
 * spends research budget, touches the holdout, or trades.
 *
 * Memoised: App keeps every prop stable between live ticks.
 */
export const ResearchPage = memo(function ResearchPage({
  client,
  labClient,
  onClose,
  onShowCandidate,
  onLoadPlan,
  testerBusy,
}: ResearchPageProps) {
  const ids = useId()
  const [tab, setTabState] = useState<ResearchTab>(loadResearchTab)
  const [journalTarget, setJournalTarget] = useState<string | null>(null)
  const setTab = (t: ResearchTab): void => {
    setTabState(t)
    saveResearchTab(t)
    setJournalTarget((target) => journalTargetAfterTabChange(target, t))
  }

  // ---- Runs: loaded once, then polled every 5 s while a run is live and making progress ----
  const { runs, error: runsError, fetchedAt, refresh: refreshRuns } = useResearchRuns(client)
  const offline = runsError?.kind === 'offline'
  // `fetchedAt` is the clock here: staleness is judged as of the last answer.
  const chip = liveChipState(runs, offline, fetchedAt ?? 0)

  // Focus the active tab on open so the keyboard lands inside the view.
  const tabRefs = useRef(new Map<ResearchTab, HTMLButtonElement>())
  useEffect(() => {
    tabRefs.current.get(loadResearchTab())?.focus()
  }, [])

  // Esc goes back to the chart, wherever focus is (the app's own shortcuts pause while open).
  useEscapeToClose(onClose)

  const tabId = (t: ResearchTab) => `${ids}-tab-${t}`
  const panelId = `${ids}-panel`

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
    e.preventDefault()
    const order = RESEARCH_TABS.map((t) => t.id)
    const i = order.indexOf(tab)
    const next =
      e.key === 'Home'
        ? order[0]
        : e.key === 'End'
          ? order[order.length - 1]
          : order[(i + (e.key === 'ArrowRight' ? 1 : order.length - 1)) % order.length]
    setTab(next)
    tabRefs.current.get(next)?.focus()
  }

  const openJournal = (name: string): void => {
    setJournalTarget(name)
    setTab('journal')
  }

  return (
    <section className="research" aria-labelledby={`${ids}-title`}>
      <header className="research__head">
        <span className="research__head-icon" aria-hidden="true">
          <ResearchIcon size={16} />
        </span>
        <h2 id={`${ids}-title`} className="research__title">
          Research
        </h2>
        <LiveChip state={chip} fetchedAt={fetchedAt} />
        <div className="research__tabs" role="tablist" aria-label="Research view">
          {RESEARCH_TABS.map((t) => (
            <button
              key={t.id}
              ref={(el) => {
                if (el) tabRefs.current.set(t.id, el)
                else tabRefs.current.delete(t.id)
              }}
              id={tabId(t.id)}
              type="button"
              role="tab"
              className="research__tab"
              aria-selected={tab === t.id}
              aria-controls={tab === t.id ? panelId : undefined}
              tabIndex={tab === t.id ? 0 : -1}
              onClick={() => setTab(t.id)}
              onKeyDown={onTabKeyDown}
            >
              {t.label}
              {t.id === 'runs' && runs !== null && runs.length > 0 && (
                <span className="research__tab-count">{fmtInt(runs.length)}</span>
              )}
            </button>
          ))}
        </div>
        <span className="research__spacer" />
        <button
          type="button"
          className="research__back"
          onClick={onClose}
          title="Back to the chart (Esc)"
        >
          <ChartIcon />
          Back to chart
        </button>
      </header>

      <div id={panelId} className="research__body" role="tabpanel" aria-labelledby={tabId(tab)}>
        {/* Keyed by tab: a tab that throws while rendering shows a small panel in its place,
            the tab bar keeps working, and picking another tab mounts a fresh boundary. */}
        <ErrorBoundary
          key={tab}
          onError={onTabError}
          fallback={({ error, reset }) => (
            <TabFallback
              tab={tab}
              error={error}
              onRetry={() => {
                // Remember this tab again; it is reset to Runs if it throws once more.
                saveResearchTab(tab)
                reset()
              }}
              onOpenRuns={() => setTab('runs')}
            />
          )}
        >
          {tab === 'runs' && (
            <RunsTab
              runs={runs}
              error={runsError}
              fetchedAt={fetchedAt}
              chip={chip}
              onRetry={refreshRuns}
              onOpenJournal={openJournal}
              onOpenCandidates={() => setTab('candidates')}
            />
          )}
          {tab === 'ideas' && <IdeasTab client={client} runs={runs ?? []} />}
          {tab === 'ledger' && <LedgerTab labClient={labClient} runs={runs ?? []} />}
          {tab === 'journal' && <JournalTab client={client} target={journalTarget} />}
          {tab === 'candidates' && (
            <CandidatesTab
              client={client}
              testerBusy={testerBusy}
              onShow={onShowCandidate}
              onLoad={onLoadPlan}
            />
          )}
        </ErrorBoundary>
      </div>
    </section>
  )
})

/** A tab threw while rendering: log it, and do not reopen the view on that tab. */
function onTabError(error: Error, info: ErrorInfo): void {
  resetResearchTab()
  console.error('A Research tab failed to render', error, info.componentStack)
}

const TAB_LABELS: Record<ResearchTab, string> = Object.fromEntries(
  RESEARCH_TABS.map((t) => [t.id, t.label]),
) as Record<ResearchTab, string>

export interface TabFallbackProps {
  tab: ResearchTab
  error: Error
  /** Mount the tab again. */
  onRetry(): void
  /** Switch to the Runs tab (not offered on Runs itself). */
  onOpenRuns(): void
}

/** Shown in a tab's place when it threw while rendering; the rest of the view keeps working. */
export function TabFallback({ tab, error, onRetry, onOpenRuns }: TabFallbackProps) {
  return (
    <div className="research__state research__state--bad" role="alert">
      <span className="research__state-icon" aria-hidden="true">
        <WarnIcon />
      </span>
      <p className="research__state-title">The {TAB_LABELS[tab]} tab failed to render</p>
      <p className="research__state-text">
        Part of the lab&apos;s answer could not be shown. The other tabs, the chart and the Strategy
        Tester are unaffected.
      </p>
      <p className="research__state-text research__mono research__fallback-detail">
        {error.message}
      </p>
      <div className="research__state-actions">
        <button type="button" className="research__btn research__btn--primary" onClick={onRetry}>
          Try again
        </button>
        {tab !== 'runs' && (
          <button type="button" className="research__btn" onClick={onOpenRuns}>
            Open Runs
          </button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------------------------

const CHIP_TEXT: Record<LiveChipState, string> = {
  live: 'Live · every 5 s',
  paused: 'Run stalled · checking every 30 s',
  idle: 'Up to date',
  offline: 'Lab offline',
}

function LiveChip({ state, fetchedAt }: { state: LiveChipState; fetchedAt: number | null }) {
  const text = CHIP_TEXT[state]
  const note = runsRefreshNote(state)
  const refreshed = fetchedAt !== null ? `Last refreshed ${fmtUtc(fetchedAt)} · ${note}` : note
  return (
    <span
      className={`research__live research__live--${state}`}
      title={
        state === 'paused'
          ? 'Auto-refresh paused: the running run has not reported progress for over 10 minutes. Use “Check again” on the run.'
          : refreshed
      }
    >
      <span className="research__live-dot" aria-hidden="true" />
      {text}
    </span>
  )
}

function Badge({
  tone,
  children,
  title,
}: {
  tone: ResearchTone
  children: ReactNode
  title?: string
}) {
  return (
    <span className={`research__badge research__badge--${tone}`} title={title}>
      {tone === 'live' && <span className="research__badge-dot" aria-hidden="true" />}
      {tone === 'calm' && <CheckIcon />}
      {children}
    </span>
  )
}

function ErrorState({ view, onRetry }: { view: ResearchErrorView; onRetry(): void }) {
  const offline = view.kind === 'offline'
  return (
    <div className={`research__state research__state--${offline ? 'offline' : 'bad'}`} role="alert">
      <span className="research__state-icon" aria-hidden="true">
        <ServerIcon />
      </span>
      <p className="research__state-title">{view.title}</p>
      {offline ? (
        <p className="research__state-text">
          Start it with <code>cd lab &amp;&amp; uv run python -m lab.api</code> (or{' '}
          <code>npm run dev</code> at the repo root). It listens on 127.0.0.1:8788; this page checks
          again every few seconds.
        </p>
      ) : (
        <p className="research__state-text">{view.message}</p>
      )}
      <button type="button" className="research__btn" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}

function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="research__state">
      <span className="research__state-icon" aria-hidden="true">
        <ResearchIcon size={24} />
      </span>
      <p className="research__state-title">{title}</p>
      <div className="research__state-text">{children}</div>
    </div>
  )
}

function Loading({ label }: { label: string }) {
  return (
    <div className="research__loading" role="status">
      <span className="research__spinner" aria-hidden="true" />
      {label}
    </div>
  )
}

type Loaded<T> =
  { key: string; kind: 'ready'; data: T } | { key: string; kind: 'error'; view: ResearchErrorView }

/** Fetches `load` whenever `key` changes; returns the result for the current key, else null. */
function useLoad<T>(key: string, load: (signal: AbortSignal) => Promise<T>): Loaded<T> | null {
  const [result, setResult] = useState<Loaded<T> | null>(null)
  const loadRef = useRef(load)
  useEffect(() => {
    loadRef.current = load
  })
  useEffect(() => {
    const controller = new AbortController()
    loadRef.current(controller.signal).then(
      (data) => setResult({ key, kind: 'ready', data }),
      (err: unknown) => {
        if (controller.signal.aborted) return
        setResult({ key, kind: 'error', view: describeResearchError(err) })
      },
    )
    return () => controller.abort()
  }, [key])
  return result?.key === key ? result : null
}

function runName(r: RunItem): string {
  return r.label.trim() || r.run_id
}

function RunSelect({
  id,
  runs,
  value,
  onChange,
  allLabel,
}: {
  id: string
  runs: readonly RunItem[]
  value: string
  onChange(v: string): void
  allLabel: string
}) {
  return (
    <select
      id={id}
      className="research__select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{allLabel}</option>
      {runs.map((r) => (
        <option key={r.run_id} value={r.run_id}>
          {runName(r)} · {fmtDate(r.created_at)}
        </option>
      ))}
    </select>
  )
}

// ---------------------------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------------------------

function RunsTab({
  runs,
  error,
  fetchedAt,
  chip,
  onRetry,
  onOpenJournal,
  onOpenCandidates,
}: {
  runs: RunItem[] | null
  error: ResearchErrorView | null
  fetchedAt: number | null
  chip: LiveChipState
  onRetry(): void
  onOpenJournal(name: string): void
  onOpenCandidates(): void
}) {
  return (
    <div className="research__runs">
      <div className="research__runs-bar" role="group" aria-label="Run list refresh">
        {runs !== null && (
          <span className="research__muted research__small">
            {fmtInt(runs.length)} {runs.length === 1 ? 'run' : 'runs'}
          </span>
        )}
        <span className="research__spacer" />
        <span
          className="research__muted research__small"
          title={fetchedAt !== null ? `Last refreshed ${fmtUtc(fetchedAt)}` : undefined}
        >
          {runsRefreshNote(chip)}
          {fetchedAt !== null && (
            <>
              <span aria-hidden="true"> · </span>
              checked {fmtClockUtc(fetchedAt)}
            </>
          )}
        </span>
        <RefreshButton label="Refresh runs" onClick={onRetry} />
      </div>
      <RunsBody
        runs={runs}
        error={error}
        fetchedAt={fetchedAt}
        onRetry={onRetry}
        onOpenJournal={onOpenJournal}
        onOpenCandidates={onOpenCandidates}
      />
    </div>
  )
}

function RunsBody({
  runs,
  error,
  fetchedAt,
  onRetry,
  onOpenJournal,
  onOpenCandidates,
}: {
  runs: RunItem[] | null
  error: ResearchErrorView | null
  fetchedAt: number | null
  onRetry(): void
  onOpenJournal(name: string): void
  onOpenCandidates(): void
}) {
  const [selected, setSelected] = useState<string | null>(null)

  if (runs === null) {
    return error ? (
      <ErrorState view={error} onRetry={onRetry} />
    ) : (
      <Loading label="Loading research runs…" />
    )
  }
  if (runs.length === 0) {
    return (
      <>
        {error && <InlineError view={error} />}
        <EmptyState title="No research runs yet">
          <p>
            Start one with <code>/research-strategy</code> in Claude Code, or{' '}
            <code>uv run python research_loop.py</code> in <code>agents/</code>. This list checks
            for new runs every 30 s, so a new run appears here within about 30 seconds (or press
            refresh); its progress then updates every 5 s.
          </p>
        </EmptyState>
      </>
    )
  }
  const current = runs.find((r) => r.run_id === selected) ?? runs[0]
  const now = fetchedAt ?? toEpochMs(current.state?.updated_at) ?? 0

  return (
    <div className="research__split">
      <ul className="research__list" aria-label="Research runs">
        {runs.map((r) => {
          const o = outcomeView(r.state?.outcome)
          const active = r.run_id === current.run_id
          return (
            <li key={r.run_id}>
              <button
                type="button"
                className="research__list-item"
                aria-current={active ? 'true' : undefined}
                onClick={() => setSelected(r.run_id)}
              >
                <span className="research__list-top">
                  <span className="research__list-title">{runName(r)}</span>
                  <Badge tone={o.tone}>{o.label}</Badge>
                </span>
                <span className="research__list-meta">
                  {r.state
                    ? `${r.state.symbols.join(', ') || EMPTY} · ${r.state.interval || EMPTY}`
                    : r.run_id}
                  <span aria-hidden="true"> · </span>
                  {fmtDate(r.created_at)}
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      <div className="research__detail">
        {error && <InlineError view={error} />}
        <RunDetail
          run={current}
          now={now}
          onRefresh={onRetry}
          onOpenJournal={onOpenJournal}
          onOpenCandidates={onOpenCandidates}
        />
      </div>
    </div>
  )
}

function InlineError({ view }: { view: ResearchErrorView }) {
  return (
    <p className="research__inline-error" role="status">
      <WarnIcon />
      {view.kind === 'offline'
        ? 'Lab service not reachable — showing the last data.'
        : view.message}
    </p>
  )
}

function RunDetail({
  run,
  now,
  onRefresh,
  onOpenJournal,
  onOpenCandidates,
}: {
  run: RunItem
  now: number
  /** Ask the lab for the run list again (auto-refresh pauses while a run looks stalled). */
  onRefresh(): void
  onOpenJournal(name: string): void
  onOpenCandidates(): void
}) {
  const state = run.state
  const o = outcomeView(state?.outcome)
  const meters = budgetMeters(run)
  const events = recentEvents(state)
  const criteria = criteriaRows(run.criteria)
  const definitions = criteriaDefinitions(run.criteria)
  const journal = journalNameFromPath(state?.journal_path ?? null)
  const stale = isStale(state, now)
  const updated = toEpochMs(state?.updated_at)

  return (
    <article className="research__run" aria-label={`Research run ${runName(run)}`}>
      <header className="research__run-head">
        <div className="research__run-id">
          <h3 className="research__run-title">{runName(run)}</h3>
          <p className="research__run-sub">
            <span className="research__mono">{run.run_id}</span>
            {state && (
              <>
                <span aria-hidden="true"> · </span>
                {state.symbols.join(', ') || EMPTY} · {state.interval || EMPTY}
              </>
            )}
            <span aria-hidden="true"> · </span>
            started {fmtDateTime(run.created_at)} UTC
          </p>
        </div>
        <Badge tone={o.tone}>{o.label}</Badge>
      </header>

      <div className={`research__outcome research__outcome--${o.tone}`}>
        <p>{o.description}</p>
        {updated !== null && (
          <p className="research__outcome-time" title={fmtUtc(updated)}>
            Last update {relativeTime(updated, now)}
          </p>
        )}
      </div>

      {stale && (
        <div className="research__inline-error research__stale" role="status">
          <WarnIcon />
          <span>
            No progress for over 10 minutes. The research loop may have stopped without writing an
            outcome, so auto-refresh is paused.
          </span>
          <button type="button" className="research__btn research__stale-btn" onClick={onRefresh}>
            Check again
          </button>
        </div>
      )}

      {state && (
        <ol className="research__steps" aria-label="Research loop phase">
          {phaseSteps(state.phase, state.outcome).map((s) => (
            <li
              key={s.id}
              className={`research__step research__step--${s.state}`}
              aria-current={s.state === 'current' ? 'step' : undefined}
            >
              <span className="research__step-dot" aria-hidden="true" />
              {s.label}
            </li>
          ))}
        </ol>
      )}

      {meters.length > 0 && (
        <section className="research__section" aria-label="Budget used">
          <h4 className="research__h4">Budget used</h4>
          <div className="research__meters">
            {meters.map((m) => (
              <div key={m.key} className={`research__meter research__meter--${m.level}`}>
                <div className="research__meter-top">
                  <span>{m.label}</span>
                  <span className="research__mono">{m.text}</span>
                </div>
                <div
                  className="research__meter-track"
                  role="meter"
                  aria-label={m.label}
                  aria-valuemin={0}
                  aria-valuemax={m.limit ?? undefined}
                  aria-valuenow={m.used ?? undefined}
                  aria-valuetext={m.text}
                >
                  <span className="research__meter-fill" style={{ width: `${m.pct ?? 0}%` }} />
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      <div className="research__run-cols">
        <section className="research__section" aria-label="Recent events">
          <h4 className="research__h4">Recent events</h4>
          {events.length === 0 ? (
            <p className="research__muted">
              {state
                ? 'No events yet.'
                : 'No progress file for this run, so there are no live events.'}
            </p>
          ) : (
            <ol className="research__events">
              {events.map((e) => (
                <li key={e.key} className="research__event">
                  <time className="research__mono research__event-time" title={e.title}>
                    {e.time}
                  </time>
                  <span className={`research__event-kind research__tone--${e.tone}`}>{e.kind}</span>
                  <span className="research__event-msg">{e.message}</span>
                </li>
              ))}
            </ol>
          )}
        </section>

        <section className="research__section" aria-label="Success criteria">
          <h4 className="research__h4">Success bar</h4>
          <p className="research__muted research__small">
            Frozen when the run started (config {shortHash(run.config_sha256, 8) || EMPTY}); never
            relaxed mid-run.
          </p>
          {criteria.length === 0 ? (
            <p className="research__muted">No criteria recorded.</p>
          ) : (
            <dl className="research__criteria">
              {criteria.map((c) => (
                <div key={c.key} className="research__criterion">
                  <dt>{c.label}</dt>
                  <dd className="research__mono">{c.value}</dd>
                </div>
              ))}
            </dl>
          )}
          {definitions.length > 0 && (
            <>
              <p className="research__defs-title">
                Definitions <span className="research__muted">(how regimes are labelled)</span>
              </p>
              <dl className="research__criteria research__criteria--defs">
                {definitions.map((c) => (
                  <div key={c.key} className="research__criterion">
                    <dt>{c.label}</dt>
                    <dd className="research__mono">{c.value}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
          <div className="research__actions">
            {state && state.candidates.length > 0 && (
              <button
                type="button"
                className="research__btn research__btn--accent"
                onClick={onOpenCandidates}
              >
                View{' '}
                {state.candidates.length === 1
                  ? 'candidate'
                  : `${state.candidates.length} candidates`}
              </button>
            )}
            {journal !== null && (
              <button
                type="button"
                className="research__btn"
                onClick={() => onOpenJournal(journal)}
              >
                Open journal entry
              </button>
            )}
          </div>
        </section>
      </div>
      <p className="research__disclaimer">
        Backtests are historical simulations, not forward-looking. A run that ends with “No edge
        found” has worked correctly.
      </p>
    </article>
  )
}

// ---------------------------------------------------------------------------------------------
// Ideas
// ---------------------------------------------------------------------------------------------

function IdeasTab({ client, runs }: { client: ResearchClient; runs: readonly RunItem[] }) {
  const ids = useId()
  const [runId, setRunId] = useState('')
  const [status, setStatus] = useState<IdeaStatus | ''>('')
  const [family, setFamily] = useState('')
  const [nonce, setNonce] = useState(0)
  const result = useLoad(`${runId}|${status}|${nonce}`, (signal) =>
    client.ideas({ runId, status }, signal),
  )
  const ideas = result?.kind === 'ready' ? result.data : null
  const families = useMemo(() => (ideas ? ideaFamilies(ideas) : []), [ideas])
  const shown = ideas ? ideas.filter((i) => family === '' || i.family === family) : null

  return (
    <div className="research__pane">
      <div className="research__toolbar" role="group" aria-label="Filter ideas">
        <label htmlFor={`${ids}-run`} className="research__label">
          Run
        </label>
        <RunSelect
          id={`${ids}-run`}
          runs={runs}
          value={runId}
          onChange={setRunId}
          allLabel="All runs"
        />
        <label htmlFor={`${ids}-status`} className="research__label">
          Status
        </label>
        <select
          id={`${ids}-status`}
          className="research__select"
          value={status}
          onChange={(e) => setStatus(e.target.value as IdeaStatus | '')}
        >
          <option value="">All statuses</option>
          {IDEA_STATUSES.map((s) => (
            <option key={s} value={s}>
              {ideaStatusView(s).label}
            </option>
          ))}
        </select>
        <label htmlFor={`${ids}-family`} className="research__label">
          Family
        </label>
        <select
          id={`${ids}-family`}
          className="research__select"
          value={family}
          onChange={(e) => setFamily(e.target.value)}
        >
          <option value="">All families</option>
          {families.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
        <span className="research__spacer" />
        {shown && (
          <span className="research__muted research__small" aria-live="polite">
            {fmtInt(shown.length)} {shown.length === 1 ? 'idea' : 'ideas'}
          </span>
        )}
        <RefreshButton label="Refresh ideas" onClick={() => setNonce((n) => n + 1)} />
      </div>

      {result === null ? (
        <Loading label="Loading the idea library…" />
      ) : result.kind === 'error' ? (
        <ErrorState view={result.view} onRetry={() => setNonce((n) => n + 1)} />
      ) : shown === null || shown.length === 0 ? (
        <EmptyState
          title={
            ideas && ideas.length > 0 ? 'No ideas match these filters' : 'The idea library is empty'
          }
        >
          <p>
            {ideas && ideas.length > 0
              ? 'Try another status or family.'
              : 'The scout adds hypotheses here, with their web sources, as a research run starts. Run /research-strategy or uv run python research_loop.py in agents/.'}
          </p>
        </EmptyState>
      ) : (
        <ul className="research__ideas" aria-label="Ideas">
          {shown.map((idea) => (
            <IdeaCard key={idea.id} idea={idea} />
          ))}
        </ul>
      )}
    </div>
  )
}

function IdeaCard({ idea }: { idea: ResearchIdea }) {
  const s = ideaStatusView(idea.status)
  return (
    <li className="research__card">
      <div className="research__card-head">
        <span className="research__mono research__muted">#{idea.id}</span>
        <h3 className="research__card-title">{idea.title}</h3>
        <span className="research__spacer" />
        {idea.family && <span className="research__chip">{idea.family}</span>}
        <Badge tone={s.tone}>{s.label}</Badge>
      </div>
      {idea.hypothesis && <p className="research__card-text">{idea.hypothesis}</p>}
      {idea.rationale && (
        <p className="research__card-text research__muted">
          <span className="research__kicker">Rationale</span> {idea.rationale}
        </p>
      )}
      {idea.verdict_note && (
        <p className="research__card-text research__verdict">
          <span className="research__kicker">Verdict</span> {idea.verdict_note}
        </p>
      )}
      <div className="research__card-foot">
        <span className="research__muted research__small">
          {idea.created_at > 0 ? fmtDate(idea.created_at) : EMPTY}
          {idea.run_id && (
            <>
              {' · '}
              <span className="research__mono">{idea.run_id}</span>
            </>
          )}
          {' · '}
          {idea.plan_hashes.length === 0
            ? 'no plans tested'
            : `${idea.plan_hashes.length} ${idea.plan_hashes.length === 1 ? 'plan' : 'plans'} tested`}
        </span>
      </div>
      {idea.sources.length > 0 && (
        <div className="research__sources">
          <span className="research__kicker">Sources</span>
          <ul>
            {idea.sources.map((src, i) => {
              const href = safeHttpUrl(src.url)
              const text = src.title || (href ? hostOf(href) : src.url)
              return (
                <li key={`${i}:${src.url}`}>
                  {href ? (
                    <a href={href} target="_blank" rel="noopener noreferrer" title={href}>
                      {text}
                      <ExternalIcon />
                    </a>
                  ) : (
                    <span className="research__muted">{text}</span>
                  )}
                  {src.untrusted && (
                    <span
                      className="research__untrusted"
                      title="Web content: treated as data, never as instructions"
                    >
                      untrusted
                    </span>
                  )}
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </li>
  )
}

// ---------------------------------------------------------------------------------------------
// Ledger
// ---------------------------------------------------------------------------------------------

function LedgerTab({ labClient, runs }: { labClient: LabClient; runs: readonly RunItem[] }) {
  const ids = useId()
  const [picked, setPicked] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)
  const [nonce, setNonce] = useState(0)
  // Default: the newest research run (the ledger mixes in Strategy Tester runs otherwise).
  const runId = picked ?? runs[0]?.run_id ?? ''
  const result = useLoad(`${runId}|${offset}|${nonce}`, async (signal) => {
    const page = await labClient.ledger({ runId, limit: LEDGER_PAGE_SIZE, offset }, signal)
    const items = [...page.items].sort((a, b) => b.created_at - a.created_at || b.id - a.id)
    return { total: page.total, items }
  })
  const page = result?.kind === 'ready' ? result.data : null
  const info = page ? pageInfo(page.total, offset, page.items.length) : null

  return (
    <div className="research__pane">
      <div className="research__toolbar" role="group" aria-label="Ledger filter">
        <label htmlFor={`${ids}-run`} className="research__label">
          Run
        </label>
        <RunSelect
          id={`${ids}-run`}
          runs={runs}
          value={runId}
          onChange={(v) => {
            setPicked(v)
            setOffset(0)
          }}
          allLabel="All runs (incl. Strategy Tester)"
        />
        <span className="research__spacer" />
        {page && (
          <span className="research__muted research__small">
            {fmtInt(page.total)} {page.total === 1 ? 'trial' : 'trials'} recorded
            {runId !== '' && ' — the Deflated Sharpe counts every one'}
          </span>
        )}
        <RefreshButton label="Refresh ledger" onClick={() => setNonce((n) => n + 1)} />
      </div>
      {result === null ? (
        <Loading label="Loading the trial ledger…" />
      ) : result.kind === 'error' ? (
        <ErrorState view={result.view} onRetry={() => setNonce((n) => n + 1)} />
      ) : page === null || page.items.length === 0 ? (
        <EmptyState title="No trials recorded">
          <p>
            Every backtest, sweep trial and walk-forward a run makes is written to the ledger and
            shows up here.
          </p>
        </EmptyState>
      ) : (
        <>
          <div className="research__table-wrap">
            <table className="research__table">
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">When (UTC)</th>
                  <th scope="col">Plan</th>
                  <th scope="col">Market</th>
                  <th scope="col">Source</th>
                  <th scope="col" className="research__num">
                    Sharpe
                  </th>
                  <th scope="col" className="research__num">
                    Return
                  </th>
                  <th scope="col" className="research__num">
                    Max DD
                  </th>
                  <th scope="col" className="research__num">
                    Trades
                  </th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((t) => (
                  <LedgerRow key={t.id} item={t} />
                ))}
              </tbody>
            </table>
          </div>
          {info && (
            <div className="research__pager">
              <span className="research__muted research__small">{info.label}</span>
              <button
                type="button"
                className="research__btn"
                disabled={!info.hasPrev}
                onClick={() => setOffset((o) => pageOffset(o, LEDGER_PAGE_SIZE, -1, page.total))}
              >
                Newer
              </button>
              <button
                type="button"
                className="research__btn"
                disabled={!info.hasNext}
                onClick={() => setOffset((o) => pageOffset(o, LEDGER_PAGE_SIZE, 1, page.total))}
              >
                Older
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function LedgerRow({ item }: { item: LedgerItem }) {
  const ret = item.total_return
  return (
    <tr>
      <td className="research__num research__muted">{item.id}</td>
      <td className="research__mono">{fmtDateTime(item.created_at)}</td>
      <td className="research__strong" title={item.plan_hash}>
        {item.plan_name || shortHash(item.plan_hash)}
      </td>
      <td>
        {item.symbol} · {item.interval}
      </td>
      <td className="research__muted">{item.source || EMPTY}</td>
      <td className="research__num">{fmtRatio(item.sharpe)}</td>
      <td
        className={`research__num ${ret !== null && ret > 0 ? 'research__tone--good' : ret !== null && ret < 0 ? 'research__tone--bad' : ''}`}
      >
        {fmtSignedPct(ret)}
      </td>
      <td className="research__num">{fmtPct(item.max_drawdown)}</td>
      <td className="research__num">{fmtInt(item.n_trades)}</td>
      <td>
        {item.status === 'error' ? (
          <span className="research__tone--bad" title={item.error ?? undefined}>
            error
          </span>
        ) : (
          <span className="research__muted">ok</span>
        )}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------------------------
// Journal
// ---------------------------------------------------------------------------------------------

function JournalTab({ client, target }: { client: ResearchClient; target: string | null }) {
  const [nonce, setNonce] = useState(0)
  const [picked, setPicked] = useState<string | null>(target)
  const list = useLoad(`list|${nonce}`, async (signal) => {
    const entries = await client.journal(signal)
    return [...entries].sort((a, b) => b.date.localeCompare(a.date))
  })
  const entries = list?.kind === 'ready' ? list.data : null
  const pick = entries === null ? null : pickJournalEntry(entries, picked)
  const selected: JournalEntry | null = pick?.kind === 'entry' ? pick.entry : null

  if (list === null) return <Loading label="Loading the research journal…" />
  if (list.kind === 'error')
    return <ErrorState view={list.view} onRetry={() => setNonce((n) => n + 1)} />
  if (entries === null || pick === null || (entries.length === 0 && pick.kind !== 'missing')) {
    return (
      <EmptyState title="The research journal is empty">
        <p>
          Each research run writes a Markdown report to <code>agents/journal/</code>: ideas tried,
          why they failed, lessons, and any candidates.
        </p>
      </EmptyState>
    )
  }
  return (
    <div className="research__split">
      <ul className="research__list" aria-label="Journal entries">
        {entries.map((e) => {
          const o = journalOutcome(e.outcome)
          return (
            <li key={e.name}>
              <button
                type="button"
                className="research__list-item"
                aria-current={e.name === selected?.name ? 'true' : undefined}
                onClick={() => setPicked(e.name)}
              >
                <span className="research__list-top">
                  <span className="research__list-title">{e.title}</span>
                  {o && <Badge tone={o.tone}>{o.label}</Badge>}
                </span>
                <span className="research__list-meta">
                  {e.date || EMPTY}
                  {e.run_id && (
                    <>
                      {' · '}
                      <span className="research__mono">{e.run_id}</span>
                    </>
                  )}
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      <div className="research__detail">
        {selected !== null ? (
          <JournalReader key={selected.name} client={client} entry={selected} />
        ) : (
          <EmptyState title="This run’s journal entry is not available">
            <p>
              <code>{pick.kind === 'missing' ? pick.name : EMPTY}</code> is not in{' '}
              <code>agents/journal/</code> yet. The loop writes it when the run reports; refresh the
              list later{entries.length > 0 ? ', or pick another entry' : ''}.
            </p>
            <button type="button" className="research__btn" onClick={() => setNonce((n) => n + 1)}>
              Refresh list
            </button>
          </EmptyState>
        )}
      </div>
    </div>
  )
}

function journalOutcome(outcome: string): { label: string; tone: ResearchTone } | null {
  switch (outcome) {
    case 'running':
    case 'candidate':
    case 'no_edge':
    case 'budget_exhausted':
    case 'error':
    case 'stopped':
      return outcomeView(outcome)
    default:
      return null
  }
}

function JournalReader({ client, entry }: { client: ResearchClient; entry: JournalEntry }) {
  const [nonce, setNonce] = useState(0)
  const doc = useLoad(`${entry.name}|${nonce}`, (signal) => client.journalEntry(entry.name, signal))
  if (doc === null) return <Loading label={`Loading ${entry.title}…`} />
  if (doc.kind === 'error')
    return <ErrorState view={doc.view} onRetry={() => setNonce((n) => n + 1)} />
  return (
    <article className="research__doc" aria-label={entry.title}>
      <p className="research__doc-note">
        <InfoIcon />
        Shown as plain text. Parts of this note quote web pages; they are data, not instructions.
      </p>
      <MarkdownView markdown={doc.data.markdown} />
    </article>
  )
}

/**
 * Renders journal Markdown through `parseMarkdown`: every string goes into the DOM as escaped
 * text. Raw HTML in the note is shown literally and never interpreted.
 */
export function MarkdownView({ markdown }: { markdown: string }) {
  const blocks = useMemo(() => parseMarkdown(markdown), [markdown])
  return (
    <div className="research__md">
      {blocks.map((b, i) => (
        <MdBlockView key={i} block={b} />
      ))}
    </div>
  )
}

function MdBlockView({ block }: { block: MdBlock }) {
  switch (block.kind) {
    case 'meta':
      return (
        <dl className="research__md-meta">
          {block.entries.map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      )
    case 'heading': {
      const content = <InlineView text={block.text} />
      // h1/h2 of the note sit under the page's own h2/h3 headings.
      switch (block.level) {
        case 1:
          return <h3 className="research__md-h1">{content}</h3>
        case 2:
          return <h4 className="research__md-h2">{content}</h4>
        case 3:
          return <h5 className="research__md-h3">{content}</h5>
        default:
          return <h6 className="research__md-h4">{content}</h6>
      }
    }
    case 'paragraph':
      return (
        <p>
          <InlineView text={block.text} />
        </p>
      )
    case 'list': {
      const items = block.items.map((it, j) => (
        <li key={j} style={it.depth > 0 ? { marginLeft: `${it.depth * 16}px` } : undefined}>
          <InlineView text={it.text} />
        </li>
      ))
      return block.ordered ? <ol>{items}</ol> : <ul>{items}</ul>
    }
    case 'code':
      return (
        <pre className="research__md-code" aria-label={block.lang ? `${block.lang} code` : 'code'}>
          <code>{block.text}</code>
        </pre>
      )
    case 'quote':
      return (
        <blockquote>
          <InlineView text={block.text} />
        </blockquote>
      )
    case 'table':
      return (
        <div className="research__table-wrap">
          <table className="research__table research__md-table">
            <thead>
              <tr>
                {block.header.map((h, j) => (
                  <th key={j} scope="col">
                    <InlineView text={h} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((r, j) => (
                <tr key={j}>
                  {r.map((c, k) => (
                    <td key={k}>
                      <InlineView text={c} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'rule':
      return <hr />
  }
}

function InlineView({ text }: { text: string }) {
  const parts: Inline[] = parseInline(text)
  return (
    <>
      {parts.map((p, i) => {
        switch (p.kind) {
          case 'text':
            return <span key={i}>{p.text}</span>
          case 'code':
            return <code key={i}>{p.text}</code>
          case 'strong':
            return <strong key={i}>{p.text}</strong>
          case 'em':
            return <em key={i}>{p.text}</em>
          case 'link':
            return p.href ? (
              <a key={i} href={p.href} target="_blank" rel="noopener noreferrer" title={p.href}>
                {p.text}
              </a>
            ) : (
              <span key={i}>{p.text}</span>
            )
        }
      })}
    </>
  )
}

// ---------------------------------------------------------------------------------------------
// Candidates (+ paper-trading comparisons)
// ---------------------------------------------------------------------------------------------

interface CandidatesData {
  candidates: ResearchCandidate[]
  paper: PaperComparison[]
  /** The paper endpoint failed while candidates loaded; comparisons are just missing. */
  paperError: string | null
}

function CandidatesTab({
  client,
  testerBusy,
  onShow,
  onLoad,
}: {
  client: ResearchClient
  testerBusy: boolean
  onShow(r: CandidateRunRequest): void
  onLoad(r: CandidateRunRequest): void
}) {
  const [nonce, setNonce] = useState(0)
  const result = useLoad<CandidatesData>(`c|${nonce}`, async (signal) => {
    const [candidates, paper] = await Promise.all([
      client.candidates(signal),
      client.paper(signal).then(
        (p) => ({ ok: true as const, p }),
        (err: unknown) => ({ ok: false as const, err }),
      ),
    ])
    if (!paper.ok) {
      const view = describeResearchError(paper.err)
      if (view.kind === 'offline' || view.kind === 'aborted') throw paper.err
      return { candidates, paper: [], paperError: view.message }
    }
    return { candidates, paper: paper.p, paperError: null }
  })

  if (result === null) return <Loading label="Loading candidates…" />
  if (result.kind === 'error')
    return <ErrorState view={result.view} onRetry={() => setNonce((n) => n + 1)} />
  const { candidates, paper, paperError } = result.data
  const hashes = new Set(candidates.map((c) => c.plan_hash))
  const orphans = unmatchedPaper(paper, hashes)

  return (
    <div className="research__pane">
      <div className="research__toolbar">
        <p className="research__muted research__small research__toolbar-text">
          {CANDIDATE_EXPLAINER}
        </p>
        <span className="research__spacer" />
        <RefreshButton label="Refresh candidates" onClick={() => setNonce((n) => n + 1)} />
      </div>
      {paperError && (
        <p className="research__inline-error" role="status">
          <WarnIcon />
          Paper-trading comparisons could not be loaded: {paperError}
        </p>
      )}
      {candidates.length === 0 ? (
        <EmptyState title="No candidates yet">
          <p>
            None of the research runs has produced a plan that clears the fixed success bar. That is
            the common, honest outcome — “no edge found” means the tests did their job.
          </p>
        </EmptyState>
      ) : (
        <ul className="research__cards" aria-label="Candidates">
          {candidates.map((c) => (
            <CandidateCard
              key={`${c.run_id}:${c.plan_hash}`}
              candidate={c}
              paper={paperFor(paper, c.plan_hash)}
              testerBusy={testerBusy}
              onShow={onShow}
              onLoad={onLoad}
            />
          ))}
        </ul>
      )}
      {orphans.length > 0 && (
        <section className="research__section" aria-label="Other paper-trading comparisons">
          <h4 className="research__h4">Other paper-trading comparisons</h4>
          {orphans.map((p, i) => (
            <PaperView key={`${p.plan_hash}:${p.generated_at ?? i}`} comparison={p} />
          ))}
        </section>
      )}
      <p className="research__disclaimer">
        Historical simulation, not forward-looking. Past performance does not predict future
        results.
      </p>
    </div>
  )
}

function CandidateCard({
  candidate,
  paper,
  testerBusy,
  onShow,
  onLoad,
}: {
  candidate: ResearchCandidate
  paper: PaperComparison[]
  testerBusy: boolean
  onShow(r: CandidateRunRequest): void
  onLoad(r: CandidateRunRequest): void
}) {
  const mapping = candidateRunRequest(candidate)
  const summary = summaryView(candidate.summary)
  const latest = paper[0] ?? null
  const req = mapping.ok ? mapping.request : null
  const showBlocked = !mapping.ok
    ? mapping.reason
    : testerBusy
      ? 'A Strategy Tester backtest is running; wait for it to finish'
      : null

  return (
    <li className="research__card research__card--candidate">
      <div className="research__card-head">
        <h3 className="research__card-title">{req?.name ?? 'Unnamed plan'}</h3>
        {req && (
          <span className="research__chip research__chip--market">
            {req.symbol} · {req.interval}
          </span>
        )}
        <span className="research__spacer" />
        {latest ? (
          <Badge
            tone={verdictView(latest.verdict).tone}
            title={verdictView(latest.verdict).description}
          >
            Paper: {verdictView(latest.verdict).label}
          </Badge>
        ) : (
          <Badge tone="neutral">Not paper-trading yet</Badge>
        )}
      </div>
      <p className="research__muted research__small">
        <span className="research__mono">{candidate.run_id || EMPTY}</span>
        {candidate.idea_id !== null && ` · idea #${candidate.idea_id}`}
        {candidate.trial_id !== null && ` · trial #${candidate.trial_id}`}
        {' · plan '}
        <span className="research__mono" title={candidate.plan_hash}>
          {shortHash(candidate.plan_hash) || EMPTY}
        </span>
      </p>
      {summary.text && <p className="research__card-text">{summary.text}</p>}
      {summary.items.length > 0 && (
        <dl className="research__stats">
          {summary.items.map((s) => (
            <div key={s.key} className="research__stat">
              <dt>{s.label}</dt>
              <dd className="research__mono">{s.value}</dd>
            </div>
          ))}
        </dl>
      )}

      <div className="research__actions">
        <button
          type="button"
          className="research__btn research__btn--primary"
          disabled={showBlocked !== null}
          title={showBlocked ?? 'Switch the chart to this market and draw the trades and equity'}
          onClick={() => req && onShow(req)}
        >
          <ChartIcon />
          Show on chart
        </button>
        <button
          type="button"
          className="research__btn"
          disabled={req === null}
          title={req ? 'Put this plan in the Strategy Tester editor without running it' : undefined}
          onClick={() => req && onLoad(req)}
        >
          Open in Strategy Tester
        </button>
        {showBlocked !== null && (
          <span className="research__muted research__small">{showBlocked}</span>
        )}
      </div>
      <p className="research__muted research__small">
        Re-running the plan records one more trial in the ledger (under the Strategy Tester&apos;s
        run).
      </p>

      {paper.length > 0 ? (
        <PaperView comparison={paper[0]} history={paper.length - 1} />
      ) : (
        <p className="research__paper-empty">
          Forward test it with <code>freqtrade trade --dry-run</code> or Binance Spot Testnet for
          4–8 weeks. Weekly comparisons with the backtest appear here.
        </p>
      )}

      <details className="research__plan">
        <summary>Plan JSON</summary>
        <pre className="research__md-code">
          <code>{req?.planText ?? JSON.stringify(candidate.plan, null, 2)}</code>
        </pre>
      </details>
    </li>
  )
}

const STAT_ROWS: readonly {
  key: keyof TradeStats
  label: string
  fmt(v: number | null): string
}[] = [
  { key: 'n_trades', label: 'Trades', fmt: (v) => fmtInt(v) },
  { key: 'total_return', label: 'Total return', fmt: (v) => fmtSignedPct(v) },
  { key: 'win_rate', label: 'Win rate', fmt: (v) => fmtPct(v, 1) },
  { key: 'mean_trade_return', label: 'Mean trade', fmt: (v) => fmtSignedPct(v) },
]

function PaperView({
  comparison: p,
  history = 0,
}: {
  comparison: PaperComparison
  history?: number
}) {
  const v = verdictView(p.verdict)
  const period =
    p.period_start !== null && p.period_end !== null
      ? `${fmtDate(p.period_start)} → ${fmtDate(p.period_end)}`
      : EMPTY
  return (
    <section
      className="research__paper"
      aria-label={`Paper trading: ${p.candidate || p.plan_hash}`}
    >
      <div className="research__paper-head">
        <span className="research__kicker">Forward vs backtest</span>
        <Badge tone={v.tone}>{v.label}</Badge>
        <span className="research__muted research__small">
          {p.candidate && `${p.candidate} · `}
          {p.symbol} {p.interval} · {period}
          {history > 0 && ` · ${history} earlier ${history === 1 ? 'report' : 'reports'}`}
        </span>
      </div>
      <p className="research__muted research__small">{v.description}</p>
      <table className="research__table research__table--paper">
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col" className="research__num">
              Forward (paper)
            </th>
            <th scope="col" className="research__num">
              Backtest
            </th>
          </tr>
        </thead>
        <tbody>
          {STAT_ROWS.map((r) => (
            <tr key={r.key}>
              <th scope="row">{r.label}</th>
              <td className="research__num">{r.fmt(p.forward[r.key])}</td>
              <td className="research__num">{r.fmt(p.backtest[r.key])}</td>
            </tr>
          ))}
          <tr>
            <th scope="row">Entries matched</th>
            <td className="research__num" colSpan={2}>
              {fmtPct(p.entry_match_rate, 0)}
            </td>
          </tr>
          <tr>
            <th scope="row">Mean entry slippage</th>
            <td className="research__num" colSpan={2}>
              {p.mean_entry_slippage_bps === null
                ? EMPTY
                : `${fmtRatio(p.mean_entry_slippage_bps, 1)} bp`}
            </td>
          </tr>
        </tbody>
      </table>
      {p.notes.length > 0 && (
        <ul className="research__notes">
          {p.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------------------------

function RefreshButton({ label, onClick }: { label: string; onClick(): void }) {
  return (
    <button
      type="button"
      className="research__icon-btn"
      aria-label={label}
      title="Refresh"
      onClick={onClick}
    >
      <svg {...SMALL}>
        <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v3h-3" />
      </svg>
    </button>
  )
}

const SMALL = {
  width: 16,
  height: 16,
  viewBox: '0 0 16 16',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

function CheckIcon() {
  return (
    <svg {...SMALL} width={12} height={12}>
      <path d="m3.5 8.5 3 3 6-7" />
    </svg>
  )
}

function InfoIcon() {
  return (
    <svg {...SMALL} width={14} height={14}>
      <circle cx="8" cy="8" r="5.8" />
      <path d="M8 7.3v3.6M8 5.2v.01" />
    </svg>
  )
}

function ServerIcon() {
  return (
    <svg {...SMALL} width={22} height={22}>
      <rect x="2.5" y="2.5" width="11" height="4.5" rx="1" />
      <rect x="2.5" y="9" width="11" height="4.5" rx="1" />
      <path d="M5 4.75h.01M5 11.25h.01" />
    </svg>
  )
}

function ExternalIcon() {
  return (
    <svg {...SMALL} width={11} height={11} className="research__ext">
      <path d="M9.5 2.5h4v4M13.5 2.5 7.5 8.5M12 9.5v3.5a.5.5 0 0 1-.5.5H3a.5.5 0 0 1-.5-.5V4.5A.5.5 0 0 1 3 4h3.5" />
    </svg>
  )
}
