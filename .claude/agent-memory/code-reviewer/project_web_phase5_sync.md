---
name: web-phase5-server-sync
description: Phase 5 web/ server integration (ServerProvider, createServerSync for watchlist/indicators) - confirmed semantics and open gaps as of 2026-10-24
metadata:
  type: project
---

Confirmed (Phase 5 review, 2026-10-24):
- ServerProvider (src/data/server.ts) reuses openReconnectingSocket; effective status = socket status, except socket 'live' + upstream 'reconnecting' -> 'reconnecting'. Upstream cleared whenever socket leaves 'live'; idle-stale silent reconnects keep it. Server endTime is UNIX seconds inclusive (same as direct provider; liveCandles passes oldest-1).
- createServerSync (src/data/persistence.ts): edits before the initial GET are never PUT; server copy wins on adoption; 404 migrates local up; GET failure = local-only for the whole session (no retry). StrictMode double-mount = two GETs, first ignored; acceptable in dev.
- liveCandles subscribes WS and fetches REST in parallel, so a bad symbol reaches the WS.

Open gaps flagged (recheck next review):
- Server closes /ws/klines with 1008 for bad symbol/interval after upgrade; openReconnectingSocket treats it as transient -> onopen 'live', close, reconnect forever, gap-fill each cycle.
- useIndicators has no `storage` listener, so a stale second tab PUTs its whole old layout to the server.
- Edits made before the initial GET resolves are silently replaced by the server copy.
- Browser JS never sees WS protocol pings, so the README "ping every 20 s" does not reset the client stale timer.

**Why:** saves re-deriving the sync state machine and status combination each review.
**How to apply:** verify against current code first; skip re-litigating confirmed semantics, check whether open gaps were fixed.
