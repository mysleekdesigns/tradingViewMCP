"""Point-in-time universe: history requirement, tradability, and a perturbation test."""

from __future__ import annotations

import pandas as pd
import pytest

from lab.data.universe import eligibility_mask, eligible_at
from tests.synthetic import make_candles, perturb_after


def universe() -> dict[str, pd.DataFrame]:
    btc = make_candles(40, seed=1, interval="1d", start="2024-01-01")
    eth = make_candles(30, seed=2, interval="1d", start="2024-01-11")  # listed 10 days later
    eth = eth.drop(eth.index[[15, 16]])  # suspension: bars absent
    eth.iloc[5, eth.columns.get_loc("volume")] = 0.0
    eth.iloc[5, eth.columns.get_loc("tradable")] = False
    return {"BTCUSDT": btc, "ETHUSDT": eth}


def test_history_requirement_counts_from_first_bar() -> None:
    data = universe()
    mask = eligibility_mask(data, min_history_days=7)
    assert list(mask.columns) == ["BTCUSDT", "ETHUSDT"]
    assert mask.dtypes.eq(bool).all()
    assert mask.index.equals(data["BTCUSDT"].index)  # ETH's dates are a subset
    t = pd.Timestamp
    assert not mask.loc[t("2024-01-07", tz="UTC"), "BTCUSDT"]
    assert mask.loc[t("2024-01-08", tz="UTC"), "BTCUSDT"]
    assert not mask.loc[t("2024-01-17", tz="UTC"), "ETHUSDT"]
    assert mask.loc[t("2024-01-18", tz="UTC"), "ETHUSDT"]


def test_gaps_and_zero_volume_bars_are_ineligible() -> None:
    data = universe()
    mask = eligibility_mask(data, min_history_days=0)
    eth = data["ETHUSDT"]
    assert not mask.loc[eth.index[5], "ETHUSDT"]  # zero volume
    assert not mask.loc[pd.Timestamp("2024-01-26", tz="UTC"), "ETHUSDT"]  # absent bar
    assert mask.loc[pd.Timestamp("2024-01-25", tz="UTC"), "ETHUSDT"]
    assert not mask.loc[pd.Timestamp("2024-01-05", tz="UTC"), "ETHUSDT"]  # before listing


def test_default_requires_a_year_of_history() -> None:
    btc = make_candles(400, seed=1, interval="1d", start="2023-01-01")
    mask = eligibility_mask({"BTCUSDT": btc})
    assert not mask["BTCUSDT"].iloc[364]
    assert mask["BTCUSDT"].iloc[365]


def test_eligibility_at_t_unchanged_when_later_data_changes() -> None:
    data = universe()
    t = 19  # BTC row position; ETH aligned via timestamps
    cut = data["BTCUSDT"].index[t]
    base = eligibility_mask(data, min_history_days=7)

    altered = {s: perturb_after(f, int((f.index <= cut).sum()) - 1) for s, f in data.items()}
    eth = altered["ETHUSDT"]
    later = eth.index > cut
    eth.loc[later, "volume"] = 0.0
    eth.loc[later, "tradable"] = False
    altered["ETHUSDT"] = eth.drop(eth.index[later][:3])  # also delete some later bars
    altered["BTCUSDT"] = altered["BTCUSDT"].iloc[: t + 5]  # and truncate (a "delisting")
    pert = eligibility_mask(altered, min_history_days=7)

    pd.testing.assert_frame_equal(base.loc[:cut], pert.loc[:cut], check_freq=False)
    assert not base.loc[cut:].equals(pert.loc[cut:].reindex(base.loc[cut:].index))


def test_eligible_at_lists_symbols() -> None:
    mask = eligibility_mask(universe(), min_history_days=7)
    assert eligible_at(mask, pd.Timestamp("2024-01-20", tz="UTC")) == ["BTCUSDT", "ETHUSDT"]
    assert eligible_at(mask, pd.Timestamp("2024-01-10", tz="UTC")) == ["BTCUSDT"]
    assert eligible_at(mask, pd.Timestamp("2024-01-20")) == ["BTCUSDT", "ETHUSDT"]  # naive = UTC
    assert eligible_at(mask, pd.Timestamp("2024-01-20 03:00", tz="UTC")) == []  # no bar there


def test_empty_and_bad_inputs() -> None:
    mask = eligibility_mask({"BTCUSDT": make_candles(5).iloc[:0]})
    assert mask.empty and list(mask.columns) == ["BTCUSDT"]
    with pytest.raises(ValueError, match="min_history_days"):
        eligibility_mask({}, min_history_days=-1)
    bad = make_candles(5).drop(columns=["tradable"])
    with pytest.raises(ValueError, match="'XRPUSDT'"):
        eligibility_mask({"XRPUSDT": bad})
