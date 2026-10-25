# Seed strategies (calibration baseline)

PLAN.md Phase 8, research loop step 0 "Seed". Each `*.json` file here is a popular open-source
Freqtrade strategy translated into a lab plan. Running them through the honest pipeline
(`uv run python seed_calibration.py` from `agents/`) does two things:

1. **Calibrates the tests.** Published, widely copied strategies are expected to FAIL the Phase 8
   success criteria (DSR, PBO, walk-forward Sharpe, benchmarks, adversarial checks). If they
   passed easily, the pipeline would be suspect.
2. **Seeds a library of known failures** the research agents can learn from.

Calibration never calls the holdout check. Results are historical simulations, not
forward-looking.

## File shape

```json
{
  "name": "human-readable name",
  "source_url": "primary source of the strategy",
  "translation_notes": "what was approximated or dropped",
  "plan": { "...": "a plan that passes lab.plan.validator.validate_plan" }
}
```

## How the translations were made

The strategies' logic was read from the primary sources and paraphrased into plan rules; no
Freqtrade code is copied (Freqtrade and freqtrade-strategies are GPL-3.0). Common changes:

- **Timeframe:** 5m/15m originals run on 1h (research keeps 15m/1h/4h archives; 1h keeps the
  runs fast and the bar count comparable across seeds).
- **ROI tables** (time-decaying take-profits) become one `take_profit_pct`: the table's
  `"0"` entry. **Trailing stops** are dropped (the lab engine has none).
- **Indicators not in the registry** (Heikin-Ashi, TEMA, Fisher-RSI, SAR, MFI, CCI, ADX,
  stochastics, candle patterns) are approximated where an honest proxy exists and dropped
  otherwise. Fisher-RSI is `tanh(0.1 x (RSI - 50))`, a monotone function of RSI, so its
  thresholds convert exactly to RSI thresholds.
- **Bollinger bands** use close; the originals use typical price.
- **Long side only:** the lab engine is long/flat spot.

Strategy004 (ADX, CCI, and two stochastics, nothing else) has no faithful translation and is
not included.
