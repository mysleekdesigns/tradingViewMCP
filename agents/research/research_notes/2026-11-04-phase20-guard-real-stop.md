---
date: 2026-11-04
phase: 20
status: passed
---

# Phase 20 circuit breaker: a real dry-run bot stopped by `guard`

The fixture tests never signal a process, so Part E's circuit breaker box stayed open until a real freqtrade bot was stopped by the guard on its own. This was that test, in the scratchpad. `--paper-dir` and `--journal-dir` pointed there throughout; `agents/data/paper`, `agents/journal` and the ledger were not touched.

**Setup.** freqtrade 2026.9, `trade --dry-run`, launched by the real `paper_trade.py prepare --launch`, which wrote `bot.pid`. Plan: BTCUSDT 5m, entry `r > 0` so a position opens at once, stake 900 of a 1000 wallet, breaker 0.05% daily and 0.05% drawdown, tight on purpose so that a real entry fee trips it. Only the protocol's registration was built by hand, the way `tests/test_paper_guard.py` does, with a synthetic design and a test ledger link, because `prepare` correctly refuses a plan with no research trial and adding a throwaway plan to the real ledger would raise the trial count for nothing. `guard --watch` then ran unattended.

| time (UTC) | event |
|---|---|
| 00:13 | Bot running, pid 96256, protections loaded |
| 00:20:04 | Real entry: market buy 0.01115 BTC at about 80,712 |
| 00:20 | Guard: clear through bar 00:15, equity 1000.00 |
| 00:25:20 | Guard, on bar 00:20 closed: `HALTED guard-proof: daily_loss 0.09%`, `sent SIGTERM to pid 96256` |
| 00:25:20 | Bot logged "worker found ... calling exit" and cleaned up in the same second |

The 0.09% matches the trade: a 0.10% fee on 900 is 0.09% of the wallet, and the rest is the price move.

| part of the box | evidence |
|---|---|
| Stops that dry-run process | SIGTERM only after the pid's real command line matched this folder's `freqtrade trade --dry-run`; process gone, no scratch process left |
| Writes `halt.lock` with reason, threshold, value, time | `{"reason": "daily_loss", "threshold": 0.05, "value": 0.0879, "time": "2026-11-05T00:20:00+00:00"}` |
| `prepare` and start refuse while the lock exists | `prepare --launch` exit 2, "paper test is halted (reason: 'daily_loss')", no process started |
| Halt logged in the test's journal | "Halted at bar 2026-11-05T00:20:00+00:00: daily_loss ..." under its own heading |
| Only the owner deletes it | The Bash and edit hooks deny it; `hooktest.sh` covered both, and the hook also stopped one of this check's own commands for naming the lock beside a `cp` |
| A repeat run changes nothing | `guard` again: exit 1, lock unchanged, still one journal line, and it says the old pid is gone |

**The HC bots were not disturbed.** All 7 ran throughout, with the same process ids before and after (53185 to 53203).

**Not tested here.** A real bot restarted by hand after a halt, where `confirm_trade_entry` should block entries; this is covered by fixture tests only. `compare` on this folder. The trip came from a deliberately tight threshold, so this proves the stop path, not that 3% and 10% are the right limits.

**Harness note.** The watcher's exit code was lost to my shell wrapper (`PIPESTATUS` under zsh); the one-shot re-run gave the real code, 1.
