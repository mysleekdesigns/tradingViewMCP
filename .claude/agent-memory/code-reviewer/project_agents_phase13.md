---
name: agents-phase13-review
description: Phase 13 agents/ + lab ledger review (rounds 1-4, 2026-10-28) — verified SDK/CLI facts, confirmed designs not to re-flag, round-3/4 fixes verified, minor open gaps, sandbox tricks
metadata:
  type: project
---

Phase 13 (Research Agents v2) review of `agents/` + `lab/lab/ledger.py` / `mcp_server.py`, 2026-10-28.

**Verified against SDK 0.2.174 / bundled CLI 2.1.310 (binary strings):**
- `ClaudeAgentOptions.env` is merged over the inherited env (subprocess_cli.py); env names
  CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH / _MAX_CONCURRENT_SUBAGENTS / ENABLE_TOOL_SEARCH exist.
- PostToolUse `updatedToolOutput` with a shape mismatch is REJECTED and the CLI uses the original
  output. WebFetch output = {bytes, code, codeText, result, durationMs, url, artifactRead?};
  WebSearch = {query, results: [str | {tool_use_id, content:[{title,url,snippet?}]}],
  durationSeconds, searchCount?}.
- ResultMessage subtype `error_max_structured_output_retries` exists.
- SDK `_find_cli` prefers the bundled CLI over PATH, so the version-pin test is meaningful.
- init message `tools` = the session's tool list (Mdt builder); NOT verified whether
  `--disallowedTools` entries are filtered from it (dry run only simulates the list).

**Confirmed designs (do not re-flag):** scout hook never raises, withholds all text on internal
error; kept metadata (url/codeText/query) screened and neutralised; invalid Designer outputs
counted per idea in sidecar context (resume-safe); InitToolsError → outcome `error`, idea stays
in_progress, call charged, resume does not re-count the hypothesis; init abort charged
unknown_charge; paper final verdict write-once (`recorded_final_verdict` short-circuit);
`_create_exclusive` (mkstemp + os.link) for protocol.json/journal; `read_ledger_link` refuses a
missing file; trial_chain only written by `Ledger.record` (single BEGIN IMMEDIATE) or tail
backfill, so the read-first `_ensure_chain` cannot miss a tail written by current code.

**Round 2 gaps, all FIXED in round 3:** `pending_critic` record in IdeaProgress.context
(round-keyed, strict JSON, cleared+persisted before acting on the verdict; resume skips Designer
and the hard-stop for it); `Ledger.open_readonly` (mode=ro URI, min_schema 4 because the real
ledger is still schema 4); `append_verdict` under `fcntl.flock` on `.<entry>.lock` + mkstemp/
fsync/os.replace; research_loop exit 3 with role-specific message; budget docstring states the
double count (intentional, do not re-flag).

**Round-3 warnings, all FIXED in round 4 (verified, tests exist):** paper CLI catches
httpx.HTTPError/sqlite3.Error -> exit 2; kline loader 429 retry (3 retries, 60 s total sleep cap
checked BEFORE sleeping, Retry-After else 2 s doubling, 418 never retried); `.gitignore`
`agents/journal/.*.lock`; Critic output stored in the round's pending record and reused on resume
(older-round records ignored by `pending_critic_of` round check; add_lessons dedupes);
`open_readonly` maps readonly/unable-to-open to a "needs a writable folder" ValueError;
comparison files via mkstemp+fsync+os.replace, recreation after a final verdict only via
exclusive os.link and only if re-judged outcome/asof/sha match (observe_paper asof =
min(requested, decision_end), so re-judging at the recorded asof is deterministic).
`ledger_link(..., cfg, loader)` with `default_loader` is parquet-only: verified offline (socket
connect patched to raise) and working on the real schema-4 ledger opened immutable (~6 s).

**Open after round 4 (minor):** `cmd_prepare` step 2 (paper_trade.py ~255) and `cmd_compare`
still miss `DataUnavailableError` (a RuntimeError from the lab parquet loader) and step 2 misses
sqlite3.Error from the ledger queries -> traceback exit 1 instead of exit 2. lag_total Check is
omitted (not appended) when a per-side lag check already failed.

**Sandbox tricks:** `uv run` fails (cache dir not writable) → use `agents/.venv/bin/python` /
`lab/.venv/bin/python -m pytest -p no:cacheprovider`. `mktemp -d` is denied → use the session
scratchpad. Read ledger with `sqlite3 "file:lab/data/ledger.db?immutable=1"` (real ledger was
still pre-migration-5 on 2026-10-28: 5587 trials, first open backfills in ~3 s; tables are
research_runs/research_evidence/trials/holdout_checks, no `runs` table). CLI binary:
`strings` to scratchpad then Python substring search.
