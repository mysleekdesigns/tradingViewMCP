---
name: web-phase19-wave4
description: Phase 19 wave 4 (startup/bundle) - lazyPanels/Suspense, InertWhile, vendor codeSplitting groups, font preloads, React Compiler annotation mode via @rolldown/plugin-babel; confirmed facts 2026-10-30 and open gaps
metadata:
  type: project
---

Confirmed (reviewed 2026-10-30; tsc/vitest/lint green, SLOW compiler suite 40/40; do not re-flag):
- Owner decisions: six font weights kept; compiler `annotation` mode on 13 shell functions only. App.tsx and drawings.ts are NOT compiled (no directive), so useViewportHeight/readMirrorForClient only matter for a future `infer`.
- @rolldown/plugin-babel is `enforce: 'pre'`, so the compiler sees raw TSX (same as the SLOW test). Build output has ~34 memo_cache_sentinel uses vs 1 at baseline.
- Compiler preset applyToEnvironmentHook = consumer 'client': dev + build compile; Vitest node env (ssr) does not. Vitest maps jsdom files to 'client' too, but the repo has no jsdom tests.
- Rolldown groups include dependencies recursively: lightweight-charts (20) must outrank lwc-accessibility (10). Verified: vendor hashes unchanged after an app-only edit; lwc-accessibility imports only the LWC chunk. Panel chunks re-hash on any app change (they import index-<hash>).
- Built index.html rewrites the /node_modules/@fontsource/... preloads to the exact hashed woff2 the CSS uses; crossorigin present.
- Lazy CSS files (Settings, Seasonals, Paper, Research, Shortcuts) contain only scoped selectors; no shell component uses their classes; keyframe names unique. Restore-notice rules moved to index.css after the `.app-notice` base.
- dialogOpen is state-based, so app shortcuts are blocked while a lazy dialog loads; dialogs capture return focus in a mount effect (after load).
- InertWhile inside the dock Suspense commits only with the real panel; equivalent to the old App effect for every research/dock order.
- Owner runs `npm run dev`; a built app only runs under `vite preview` (server serves no web/dist), so vite:preloadError matters only there.

Review-fix round (2026-10-30, tsc/lint/541 tests green, scratch build checked):
- Closed: root ErrorBoundary in main.tsx (renders children only); ResearchFallback + icons + useEscapeToClose in main chunk; LazyBoundary contains only LazyLoadError and rethrows others from render (reaches Research's ErrorBoundary, documented React behaviour); retryableLazy = fresh lazy() per attempt, retry only on click, keyed remount.
- Closed: rail preload calls import() directly; vite:preloadError gated on panelLoadInFlight (counter inc/dec in the lazy factory's try/finally; React calls the factory once per lazy, so StrictMode/unmount cannot skew it; event fires inside __vitePreload before load() rejects).
- CSS: BacktestShell.css (grid rows, .tester frame, .bt-pane-legend) in main CSS at Chart.tsx's old import spot; Backtest.css now only .tester__*/.tester descendant rules (no shared selectors), so later load order is harmless. Main chunk 150.7 KB gzip.

Open:
- LazyBoundary drops a failed lazy only via its own Retry/Close: unmounting it any other way (rail toggle of dock/Research, switching sidebar panel) leaves the rejected lazy, so the next open shows "Couldn't load" instantly with no download. Fix: retry in componentWillUnmount when caught is a LazyLoadError.
- LazyLoadFailed overlay/dialog Escape is a container onKeyDown (dead once focus leaves the card); Retry drops focus to body while the fallback shows.
- A hung real load keeps panelLoadInFlight true, so a later failed hover preload would reload (edge).

**Why:** saves re-deriving the bundle/compiler facts in wave 5 and later compiler widening.
**How to apply:** start later Phase 19 / compiler `infer` reviews here; related [[web-phase19-wave3]], [[web-phase19-wave1]], [[web-toolchain-conventions]].
