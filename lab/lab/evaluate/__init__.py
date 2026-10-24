"""Benchmarks and adversarial checks that every strategy result is judged against.

Results are historical and not forward-looking.
"""

from lab.evaluate.adversarial import (
    AdversarialReport,
    SignalFn,
    VariantResult,
    adversarial_checks,
    adversarial_checks_signals,
    delay_check,
    permute_history,
    plan_signal_fn,
    run_variant,
    trade_equity_returns,
)
from lab.evaluate.benchmarks import (
    VOL_TARGET_MIN_REBALANCE,
    RandomEntryResult,
    buy_and_hold,
    random_entry_null,
    random_entry_signals,
    vol_target_buy_and_hold,
    vol_target_weights,
)

__all__ = [
    "VOL_TARGET_MIN_REBALANCE",
    "AdversarialReport",
    "RandomEntryResult",
    "SignalFn",
    "VariantResult",
    "adversarial_checks",
    "adversarial_checks_signals",
    "buy_and_hold",
    "delay_check",
    "permute_history",
    "plan_signal_fn",
    "random_entry_null",
    "random_entry_signals",
    "run_variant",
    "trade_equity_returns",
    "vol_target_buy_and_hold",
    "vol_target_weights",
]
