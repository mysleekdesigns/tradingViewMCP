---
date: 2026-11-04
status: proposal for the owner; section 3 applied 2026-11-04, sections 1 and 2 not yet
---

# Phase 20 lab lock proposal

Parts B, C, D and G of Phase 20 touch `lab/lab/registry/`, `lab/lab/research/` and `lab/config/`, which are read-only
to Claude under `.claude/lab.lock`. This file lists what the owner needs to decide or apply. Evidence for section 1 is
in `Claude Code regime trading bot video research.md` section 5. PLAN.md's checklist stays the specification; only
decisions and deviations are listed here.

## 1. Settings for Part B (owner accepted 2026-11-04)

The owner accepted the settings below, and PLAN.md now states them. No value here has been looked at against our data,
and none may be changed after seeing a result.

| Setting | PLAN.md before | Published evidence | Accepted |
|---|---|---|---|
| Number of states | "3, from the source" | 2 in every decision-driving source read | **2** |
| Bear overlay threshold | Example `hmm_bear_prob > 0.5` | 92% confidence (Nystrup 2014), author-chosen | One of the two slate entries uses **0.92** |
| Persistence | Example `persist(..., 3)` | Video's 3 bars; literature uses a confidence filter or a median of 6 predictions | The other slate entry keeps `persist(hmm_bear_prob > 0.5, 3)`, labelled video-sourced |
| Training window | Fixed in config | 500-day effective, or 1,000-day rolling (daily equities) | **3,000 bars** on 4h (the same 500-calendar-day span). It is an assumption and is stated as one |
| Refit cadence | Every 42 bars (weekly on 4h) | Adaptive with forgetting, or monthly | Keep **42** |

With two states, the earlier slate example ("flat while `hmm_state` is the highest-volatility state") would often be the
same state as the bear state, so it is dropped and the slate is the two threshold and persistence rows above.

## 2. Parts B, C, D, G

Apply as written in PLAN.md, with the settings from section 1. Two reminders:

- The registry docstring in `lab/lab/registry/regime.py` says `hmm_bear_prob` is deliberately absent. Update it when
  the feature is added.
- The Markov ribbon has no probability to draw, because its labels are a threshold on trailing return. Part H's opacity
  now belongs to the "(after Part B)" ribbon picker item and applies to the HMM source only.

## 3. Protected files only the owner can edit

**`.claude/hooks/`.** Add `halt.lock` deletion or overwrite to the Bash and edit denials, so "only the owner deletes it,
outside Claude Code" is enforced and not just a convention. No current hook mentions `halt.lock`.

**`.claude/skills/honest-backtesting/` checklist.** Add two lines:

- Sizing: state risk per trade, the exposure cap, and the largest single loss as a percentage of the wallet. Sizing does
  not change whether the plan has an edge.
- Correlation: confirmations across correlated coins are one piece of evidence, not N. Report N_eff next to per-coin
  verdicts, quoting the figure from the coins' own daily returns, because mostly-flat bots read as falsely independent.
