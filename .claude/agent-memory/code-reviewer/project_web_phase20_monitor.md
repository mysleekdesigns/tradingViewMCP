---
name: web-phase20-monitor
description: Phase 20 Part H paper monitor and regime legend - confirmed sound parts after two review rounds (2026-11-04), minor open items, how to test WAL and the real agents writer in the sandbox
metadata:
  type: project
---

Confirmed across the 2026-11-04 reviews (do not re-flag):
- Read-only better-sqlite3 over a live WAL DB, tested from a separate process: main file unchanged, writer and checkpoint unaffected, no fd growth. Sync read is no event-loop concern.
- Round 1 warnings all fixed in round 2: unknown controls show a dash (`read.protocol` + `malformed`), figures gate on `read.trades`, `unixSeconds` bound 1e11, visibilitychange poll with `inFlight`, rows keyed by folder id.
- `protectionLock`: live ONDO DB confirms the literal 'Auto lock', 26-char `lock_end_time` text and UTC. Reads stop after a failed trades read: a locked DB measured 226-229 ms in total, one wait.
- Real agents writer run through the route: `circuit_breaker.wallet` is ignored as an extra key; `halt.lock` 0600 comes from NamedTemporaryFile plus hard link and reads fine as the same user. `paper_trade.py` refuses a `--wallet` that contradicts the frozen wallet, so config.json matches the breaker.
- useRegime scopes points by `series`, so a symbol change cannot show the old symbol's run. Layouts are only 1, 2 and 4 cells.

Open after round 2 (check the code before re-reporting):
- The locked-DB test bound (busy + 2000 ms) would not catch a doubled wait.
- Ribbon opacity from filtered probability (PLAN Part H first item) not implemented; that box must stay unticked.
- Regime chip keeps key and params in the error state, so a lab-offline multi cell stays wide. RegimeChip has no render test.

**Why:** saves repeating the WAL, writer-contract and chunk verification on later monitor changes.
**How to apply:** in this sandbox tsx fails (EPERM on its IPC pipe) and `uv` cannot open its cache: use `node --no-warnings` on .mts scripts and `agents/.venv/bin/python` with PYTHONPATH=. from agents/. Headless Chrome also fails here (socket dir denied). Copy live data to the scratchpad instead of opening a running bot's files.
