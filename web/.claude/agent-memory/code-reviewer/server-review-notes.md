---
name: server-review-notes
description: server/ (Fastify + SQLite + hub + alerts) review conventions and open risks after the Phase 6 alerts review
metadata:
  type: project
---

Status after the Phase 6 server review (2026-10-24).

Open after Phase 6:
- Alert engine `armed` (greater/less edge trigger) lives only in memory. Every server restart (`npm run dev` is `tsx watch`, so every server-file save) re-fires an `every` greater/less alert whose condition still holds, once the 60 s cooldown seeded from `last_triggered_at` has passed.
- /ws/alerts never reports upstream tickers status, so the browser can't tell when alerts aren't being evaluated.
- Webhook payload sends the user note in Discord `content` with no `allowed_mentions`. No concurrency cap on osascript/webhook fan-out when many alerts fire together.
- REST mutations have no Host/Origin check (only WS routes check Origin). CSRF is blocked in practice: JSON needs a preflight, and a text/plain body arrives as a string, which `bodyObject` rejects. DNS rebinding is still open, same as the Phase 5 persistence routes.

Confirmed conventions (do not re-flag):
- The hub is created in buildApp and closed in the root onClose. The alerts plugin's onClose runs socket cleanups, then engine.close(). Either onClose order is safe: a release after hub.close is a no-op, and a grace timer set before hub.close is cleared by teardown.
- Hub release keeps a 5 s grace before teardown. The engine acquires TICKERS_KEY only while active alerts exist (`setWatching` guards against a double acquire).
- better-sqlite3 is synchronous, so a DELETE or PATCH cannot interleave with onTickers. recordTrigger also re-checks `active = 1` inside a transaction.
- Webhook errors log only err.name or the HTTP status, never the URL. osascript runs through execFile with an args array. Escaping covers `\` and `"`, and control characters are removed.
- Alert design is intentional: live last price, not bar close; crosses are strict on prev; greater/less fire on the first observation.

**Why:** these are the server risks most likely to come back in Phase 9 (paper trading, indicator alerts).
**How to apply:** in later server/ reviews, check the engine's arming persistence and the alert status signal before writing new findings. Move items to Fixed once they are fixed.
