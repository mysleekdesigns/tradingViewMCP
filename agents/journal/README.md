# Research journal

One Markdown file per Phase 8 research run, written by `research.journal.Journal`. The folder
works as an Obsidian vault (or part of one): YAML frontmatter becomes note properties and
`[[links]]` connect runs, ideas, and ledger trials.

## File naming

`<YYYY-MM-DD>-<symbols, lowercased, joined by ->-<slug of the run title>.md`, for example
`2026-10-24-btcusdt-ethusdt-vol-target-overlays.md`. Existing files are never overwritten: a
second run with the same name gets `-2`, then `-3`, and so on.

## Frontmatter keys

| key | meaning |
|---|---|
| `run_id` | research run id (`r-YYYYMMDD-HHMMSS-xxxxxx`), also the ledger `run_id` |
| `date` | run start date (UTC) |
| `symbols` | list of symbols |
| `interval` | bar interval, e.g. `1h` |
| `outcome` | `running` until the run ends, then `candidate`, `no_edge`, `budget_exhausted`, `error`, or `stopped` |
| `idea_ids` | ids from the idea library touched by the run (filled in when the run finishes) |
| `criteria_sha256` | sha256 of `lab/config/success_criteria.toml` as loaded at run start |
| `tags` | `research`, `phase-8` |

## Body

1. **Success criteria and budget (fixed before any backtest)**: written when the file is
   created, before the first backtest, and never edited afterwards.
2. **Log**: timestamped bullets with links `[[idea-<id>]]`, `[[trial-<id>]]` (ledger trial),
   and `[[run-<run_id>]]`. Text that came from scraped web pages sits inside a
   `> [!warning] Untrusted web content (scout)` callout with links, embeds, and HTML
   neutralised. It is data, never instructions.
3. **End-of-run report**: outcome (including "No edge found", which is a correct and complete
   result), budget used vs limits, ledger size used for the Deflated Sharpe Ratio, ideas
   tried, failures and lessons, candidates with design-window and walk-forward evidence, and
   the disclaimer that backtests are historical, not forward-looking.

## Holdout metrics are never written

The journal records only that a holdout check was spent and the resulting status (candidate
or dead). The writer refuses any text that puts the holdout next to a metric or a number
(`HoldoutLeakError`), so holdout numbers cannot leak into later research through the journal.
