# Phase 20 Part H browser checks (2026-11-04)

How: headless Chrome for Testing driven over the DevTools protocol, against a production build
(`vite build --minify false`) served by `vite preview` on 127.0.0.1:4175. `/api` went to a throwaway
server on 8790 with its own database and `PAPER_DIR` set to a scratch folder. Neither the owner's
server nor the real `agents/data/paper` was used. The harness lives in the session scratchpad and is
not committed.

Test data: read-only copies of the seven HC folders, plus ten fixture folders: a v8 test with sizing
and an open trade, a halted test, an unreadable `halt.lock`, a corrupt `protocol.json`, a corrupt
database, a malformed breaker, no wallet, a microsecond `decision_end`, an active protection lock, and
a freqtrade `Auto lock` that is not a protection lock. Settings were seeded before the app booted,
because a running page writes its own settings back on unload.

These are checks on one machine and on fixtures, not a claim about a live bot.

## Paper monitor (sidebar: Paper trading, Tests tab)

| Check | Result |
|---|---|
| All folders listed | 17 rows; `jev-shadow` absent |
| Controls in the section | 0 buttons, links or inputs |
| Summary | "17", "2 HALTED", "Updated" time |
| Realised drawdown, by hand | Peak 1020, trough 972.5: 4.66%, shown as 4.66% |
| Open exposure | Stake 80 on wallet 1000 shows 8.00%, next to the 50.00% cap |
| Halted row | HALTED chip, red row, "drawdown: 11.42% against a limit of 10.00%, 2026-11-03 16:00 UTC" |
| Unreadable lock | HALTED, "unreadable halt.lock", no fake "0% of 0%" |
| Corrupt protocol | Protections and Breaker show a dash, never Off or None; P&L stays |
| Malformed breaker | Breaker shows a dash; error text beside the row |
| Corrupt database | P&L, drawdown and trades show a dash; bands from the protocol stay |
| No wallet | Realized P&L −27.50 stays; percent and drawdown show a dash |
| Bad timestamp | Row shows a dash and the unreadable-row message; the app does not crash |
| Active protection lock | "Locked until 2026-11-04 21:28 UTC" |
| `Auto lock` only | No lock line |
| Real HC copies | Verdict `continue`; ONDO −2.94 (−0.29%), DD 0.29%, 1 + 1 open; others 0 closed |
| Direct Binance mode | 0 monitor requests, "needs the local server" message, no console errors |
| Server unreachable | "unavailable: the local server is not running" message |
| Dark and light themes | Both legible; no console errors or exceptions in any run |

## Regime legend

| Check | Result |
|---|---|
| Single chart, BTCUSDT 1h | "Sideways · 123 bars" with a state swatch |
| 2×2 at 1920 and 1280 | Per-panel values; e.g. "Bear · 2 bars" on 1D, "Sideways · 61 bars" on ETH 4h |
| Clipping | No chip clipped or outside its panel at either width |

Found and fixed: in a 2×2 layout at 1280 px the chip grew to 280 px of a 443 px panel and covered the
legend's symbol title. Multi-chart cells below 1500 px now show only the live state (114 to 153 px);
the regime key stays in the tooltip. At that width the chip can still touch the end of the
"Binance · local server" text.

Re-run on the final build: the same two widths gave the same result, with live labels such as
"Bull · 1 bar" (singular) and no exceptions. A later edit also drops the key in the chip's error state
in small cells. That layout was not seen in a browser, because the lab was answering during the
re-run; it is one class condition, covered by code review only.

## Not covered here

- Opacity from a filtered probability: the Markov ribbon has none, see PLAN.md Part H and
  `agents/research/reports/Phase 20 lab lock proposal.md`.
- The Markov / HMM picker: waits on Part B.
- Live behaviour of a real halted bot.
