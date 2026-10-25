"""Paper trading for research candidates: freqtrade ``trade --dry-run`` only, plus the weekly
forward-vs-backtest comparison. Nothing here places real orders or reads exchange keys.

Freqtrade (GPL-3.0) runs as a separate process in its own environment; this package never
imports it. Results are historical, not forward-looking.
"""
