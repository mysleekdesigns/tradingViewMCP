---
paths:
  - "web/**/*.test.ts"
  - "web/**/*.test.tsx"
  - "server/**/*.test.ts"
  - "lab/tests/**"
  - "agents/tests/**"
---

# Testing

- Tests run offline. No exchange, web, Anthropic, or Jev calls in the default run. Use recorded fixtures or synthetic candles generated in the test with an explicit seed.
- Vitest for `web/` and `server/`; pytest for `lab/` and `agents/`. One behaviour per test, named for the behaviour.
- Indicator tests compare against published or hand-calculated values and state the tolerance and why.
- Engine accounting tests use tiny hand-calculated examples (3–10 bars) where every fill, fee, and P&L number can be checked by hand.
- DSR/PBO tests reproduce published worked examples.
- Fixtures under `*/tests/fixtures/` are frozen once created. Add new ones; never overwrite.
- Never add `skip`, `xfail`, `.only`, `.skip`, or widen a tolerance to make a failure disappear. Find the cause.
- Slow tests (permutation nulls run through the backtest pipeline, anything over ~1 s, live-API integration) are marked and excluded from the default run.
