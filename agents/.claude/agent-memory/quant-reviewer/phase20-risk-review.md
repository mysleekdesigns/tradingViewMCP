---
name: phase20-risk-review
description: Phase 20 Parts E/E2/F reviews r1-r4 (2026-11-04, targeted: verdict ordering only, still uncommitted, no v8 protocol on disk). r3's C and W are closed:
a fail is never demoted. 324 real `compare` runs on tmp worlds (3 loser kinds x stake 1.0/1.3/0.7 x bridge ok/raise
x early/inside/past x no edit/config/strategy x trip): 0 pass, no recorded verdict moved on re-run. Non-fail only in
the two accepted cases (bridge past grace with replay-only fail; void past grace), both named in the journal line;
neither reaches pass or a step (step, new name, re-prepare, deleted folder all refused). Edit + trip stays exit 2
until the grace, then fail: matches the printed rule. Seven HC identical HEAD vs tree. Scratch is in q4/.

Open after r4:
- W: `_db_path` (paper_trade.py:277) trusts `meta.json`, outside every freeze. A loser with `db_path` pointed at a
  clean DB records `pass` (config db_url alone gives exit 2). Same at HEAD. Fix: v8 refuses/voids when meta differs.

---
r3 record:

r3 (2026-11-04) - open findings on the final inconclusive outranking fails and parked tests; what was verified clean
metadata:
  type: project
---

r3 (2026-11-04, after fix round 2, still uncommitted, no v8 protocol on disk, 795 paper tests green). All r2
items closed: masked replay, cap, plan_hash step, whole-file config. Re-check each open item before repeating it.

Open after r3:
- C: `observed.unjudged` outranks every fail (protocol.py evaluate). The same losing bot is `fail` with the bridge
  working and `inconclusive` with a stake off or freqtrade off PATH; also_failing goes in the line only. A stake
  mismatch leaves the replay valid, so it demotes nothing honestly. Before the grace it also hides early fails
  (exit 2). Frozen rule text says "fail if any rule fails". Fix: demote only replay-dependent checks, and only
  for a bridge failure.
- W: `_risk_frozen_problem` refuses before `_judge`, no grace: one edit parks a loser at pending; the "void, use a
  new candidate name" advice is refused by the duplicate check.
- Suggestions: a re-prepare rewrites strategy, plan, meta too and leaves no trace; a step may drop the breaker;
  a raised floor nobody reaches counts as a step; permanent 403 loops forever; stale bot.pid reads "not running".

Verified clean at r3: real freqtrade 2026.9, 3 plans, 1h, 2 months, masked replay equals freqtrade's entries
(28/28, 39/39, 34 of 35; the extra is a lab stop one bar earlier, an irregular, not an excuse); no freqtrade
entry strictly inside a lock, 1-2 per plan on a start bar; truncating the window leaves locks and replay
unchanged (causal). Seven HC: protocol, strategy, config, compare JSON, journals identical HEAD vs tree. Live HC
stake gaps (5 fills) all -0.01 to -0.33%, supporting the one-sided tolerance. Scratch for r3 is in q3/.

---
r2 record:

r2 (2026-11-04, after the fix round, still uncommitted, no v8 protocol on disk, 1446 agents tests green). All
r1 items closed. Re-check each open item before repeating it.

Open after r2:
- C: `check_size_step` compares `logic_hash`, which drops every number, the stop and the interval. A re-tuned
  plan with a tighter stop registered as a "step" (exit 0), real stake 50 -> 200 (4x) while `_real_sizes` all
  read 2x. The same plan at the same size also registers: no lower bound. Fix: `plan_hash` equality, and at
  least one real size strictly larger. A failed prior is still refused before the duplicate check.
- W: `config.json` is outside the freeze for protections-only and breaker-only tests. Real freqtrade: removed
  entries 1 -> 3/6/9 of 29 on fee / wallet / stake edits, and `compare` said nothing. A hand edit survives a
  plain re-prepare; deleting the file reopens `--stake-amount`.
- W: removal share has no cap and is absent from the journal line. 46% removed passes 11/27, 68% passes 4/27
  on 2 kept entries.
- W: entries that stay true (level rules) false-fail: a bot equal to the backtest has 8-13 irregular entries
  against an allowance of 4-6, and late ones read "possible look-ahead". Crossover plans are fine.
- W: stake mismatch without a breaker is exit 2 forever: no final verdict 60 days after decision, so a test
  past the 20% funding headroom stays `pending`.
- Suggestions: stake tolerance could be one-sided (real gaps all <= 0); 418 reads as permanent to `--watch`.

Verified clean at r2: seven HC protocols, strategies, configs, comparison JSON and journals identical under
HEAD and the working tree (tmp copies); a stray halt.lock leaves v7 verdicts alone; lock boundaries match real
fills (none inside a lock, freqtrade uses `lock_end_time > now`); backtest coverage check works on truncated
real data and on end=now; `max_stake` is never divided by max_open_trades; sized_stake verbatim with 0 of
20000 mismatches and low vol never above base; `write_halt_lock` race clean, mode 0600, server reads it as the
same user; guard P&L equals freqtrade's `close_profit_abs`; shadow files untouched.

**Why:** Part E/E2 controls apply only to tests started after this phase, so all of this is fixable before
the first v8 protocol is registered; afterwards each v8 protocol's frozen behaviour is a limitation.
**How to apply:** on re-review check whether any v8 protocol exists under agents/data/paper first. Repeat
the real-freqtrade check from a scratch folder (see [[review-conventions]]).
