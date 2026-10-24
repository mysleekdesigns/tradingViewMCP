---
name: web-toolchain-conventions
description: Confirmed web/ toolchain facts (TS 6 strict default, ESLint flat + Prettier, Vitest node env) so reviews don't re-flag them
metadata:
  type: project
---

web/ scaffold (Phase 0, reviewed 2026-10-24): create-vite react-ts, oxlint swapped for ESLint 10 flat config + typescript-eslint 8 + eslint-config-prettier + react-hooks 7 (includes React Compiler rules like refs, set-state-in-effect). TypeScript ~6.0 — TS 6 defaults `strict: true`, so tsconfig.node.json lacking `strict` is NOT a gap (verified with tsc). typescript-eslint peer caps TS <6.1, so the tilde pin matters.

**Why:** avoid false positives on template defaults in later reviews.
**How to apply:** Vitest runs with no config (node env, no jsdom); flag only if a change adds DOM/chart component tests. Lint is not type-aware (no no-floating-promises) — worth re-raising when Phase 1-2 adds async WS/fetch code. See [[recurring-review-issues]].
