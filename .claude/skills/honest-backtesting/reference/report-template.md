# Strategy report template

```markdown
## <plan name>: <symbol> <interval>  (design <start>–<end>, costs <fee>% + <slip>% per side)

Hypothesis: <one sentence>. Rationale: <who is on the other side>.
Plan hash: <hash>. Features: <list>. Tunable params: <n>.

| metric (walk-forward OOS) | gross | net | net ×2 cost | buy&hold | vol-tgt B&H | random null (median) |
|---|---|---|---|---|---|---|
| CAGR | | | | | | |
| Sharpe (365d) | | | | | | |
| Sortino | | | | | | |
| max DD | | | | | | |
| win rate | | | | | | |
| profit factor | | | | | | |
| trades | | | | | | |
| exposure % | | | | | | |
| turnover /yr | | | | | | |

Ledger trials this run: <N>.  DSR: <x> (≥ 0.95?).  PBO: <y> (≤ 0.2?).  Bootstrap 95% CI (ann. return): [<lo>, <hi>].
Permutation null percentile: <p> (N=<n>).
Signal delay Sharpe: 0 bars <a> → 1 <b> → 3 <c> → 5 <d>.  Sign flip Sharpe: <e>.
Parameter neighbours (±20%) profitable: <k>/<m>.
Regimes (bull / bear / sideways) net Sharpe: <..> / <..> / <..>.
Top-5 trades removed: net Sharpe <z>.
Holdout (<start>–<end>): <not checked | Sharpe net, max DD>  (holdout checks used: <u>/<budget>).
Cross-check engine (<freqtrade|nautilus>): <not run | matches within ...>.

Success criteria: <k>/<total> pass. Verdict: <candidate | refine | abandoned: reason>.
Backtests are historical, not forward-looking.
```

## End-of-run report

```markdown
# Research run <id>  (<date>, budget: <hyp> hypotheses / <prop> proposals / <bt> backtests / <ho> holdout checks / $<cap>)

Outcome: <N candidates | no edge found>.
Used: <..> hypotheses, <..> proposals, <..> backtests (ledger N), <..> holdout checks, $<..>.

| hypothesis | family | best walk-forward net Sharpe | DSR | PBO | verdict | why |
|---|---|---|---|---|---|---|

Lessons (for the next run):
- ...

Candidates entering paper trading: <none | list with plan hashes>.
Backtests are historical, not forward-looking.
```
