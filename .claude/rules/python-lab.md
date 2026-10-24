---
paths:
  - "lab/**/*.py"
  - "agents/**/*.py"
  - "**/pyproject.toml"
---

# Python (lab + agents)

- Python 3.12 via `uv`. Type hints on every public function. `ruff check` and `ruff format` clean; fix findings instead of adding `# noqa`.
- `lab/lab/engine/`, `lab/lab/registry/`, and `lab/lab/stats/` are pure: arrays/DataFrames in, arrays/DataFrames out, no I/O, no globals, no prints. Data loading, the ledger, FastAPI, and MCP live above them.
- Reuse `hedgeFund` as an editable dependency (`uv add --editable ../hedgeFund`), never by copying code. Before calling any hedgeFund function on intraday crypto, check it for daily-bar or 252-day assumptions and wrap it.
- Random seeds are explicit parameters. Constants that affect results (fees, slippage, split dates, thresholds) come from config with a one-line justification.
- Raise `ValueError` naming the bad input. Plan-validator errors must be specific enough for an agent to fix its own plan from them.
- Optional heavy deps (`nautilus_trader`, `freqtrade`) are imported lazily and degrade with a clear "not installed" result.
- The Agent SDK orchestrator and Jev gates live in `agents/`. Every Jev gate fails open (falls back to Claude) and never blocks the loop. Arithmetic, dates, and counting stay in Python, never in Jev.
