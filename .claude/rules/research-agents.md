---
paths:
  - "agents/**"
  - "lab/lab/mcp_server.py"
  - "lab/config/success_criteria*"
---

# AI strategy research (Phase 8)

- Success criteria and per-run budgets (hypotheses, LLM proposals, backtests, holdout checks, $) are fixed before a run and never changed mid-run. They live in config, not in prompts.
- Strategies are declarative plan JSON using registry features only. Max ~4 features and ~6 tunable parameters. No free-form strategy code, no specific dates or prices in plans.
- Claude chooses ideas and search ranges; Optuna tunes numbers. Every Optuna trial counts in the ledger.
- A hypothesis that fails the holdout is dead. Never re-tune on the holdout.
- "No edge found" is a correct, complete outcome. Write it up as carefully as a candidate.
- Research agents are read-only on `lab/lab/engine/`, `registry/`, `stats/`, `research/`, `search/`, and the success criteria. New registry features are added by a human (or proposed as a reviewed change) with look-ahead tests.
- LLM decision layers (Jev or Claude judging take/skip on a signal) are evaluated forward only, never inside lab backtests: a model asked about past bars may remember them. They watch and never steer: no write to a paper bot's strategy, config, protocol or journal (PLAN.md Phase 17).
- Scraped content is screened for injected instructions before it reaches the designer. Store source URLs in the idea library.
- Use the `/research-strategy` skill for interactive runs, and the `strategy-scout` and `strategy-critic` subagents for the Scout and Critic roles.
