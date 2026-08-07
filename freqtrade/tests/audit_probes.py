from __future__ import annotations
"""Deterministic strategy smoke probe; never connects to an exchange."""

import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRATEGY = ROOT / "user_data" / "strategies" / "IctSmcStrategy.py"


def load_strategy_class():
    import talib.abstract as talib_abstract

    freqtrade = types.ModuleType("freqtrade")
    strategy_module = types.ModuleType("freqtrade.strategy")

    class IStrategy:
        pass

    def informative(_timeframe):
        def decorate(function):
            return function
        return decorate

    strategy_module.IStrategy = IStrategy
    strategy_module.informative = informative
    vendor = types.ModuleType("freqtrade.vendor")
    qtpylib_pkg = types.ModuleType("freqtrade.vendor.qtpylib")
    indicators = types.ModuleType("freqtrade.vendor.qtpylib.indicators")

    def rolling_vwap(dataframe, window=14, min_periods=None):
        minimum = window if min_periods is None else min_periods
        typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
        weighted = (typical * dataframe["volume"]).rolling(
            window=window, min_periods=minimum
        ).sum()
        volume = dataframe["volume"].rolling(
            window=window, min_periods=minimum
        ).sum()
        return weighted / volume

    indicators.rolling_vwap = rolling_vwap
    qtpylib_pkg.indicators = indicators
    talib = types.ModuleType("talib")
    talib.abstract = talib_abstract
    sys.modules.update({
        "freqtrade": freqtrade,
        "freqtrade.strategy": strategy_module,
        "freqtrade.vendor": vendor,
        "freqtrade.vendor.qtpylib": qtpylib_pkg,
        "freqtrade.vendor.qtpylib.indicators": indicators,
        "talib": talib,
        "talib.abstract": talib_abstract,
    })
    namespace = {"__file__": str(STRATEGY), "__name__": "audit_strategy"}
    exec(compile(STRATEGY.read_bytes(), str(STRATEGY), "exec"), namespace)
    return namespace["IctSmcStrategy"]


def strategy_smoke() -> dict:
    import numpy as np
    import pandas as pd

    strategy_class = load_strategy_class()
    strategy = strategy_class.__new__(strategy_class)
    count = 6000
    rng = np.random.default_rng(20260713)
    dates = pd.date_range("2025-01-01", periods=count, freq="min", tz="UTC")
    trend = np.linspace(100.0, 145.0, count)
    wave = 1.8 * np.sin(np.arange(count) / 42.0) + 0.5 * np.sin(
        np.arange(count) / 7.0
    )
    close = trend + wave
    open_ = np.r_[close[0], close[:-1]]
    raw = pd.DataFrame({
        "date": dates,
        "open": open_,
        "high": np.maximum(open_, close) + 0.20,
        "low": np.minimum(open_, close) - 0.20,
        "close": close,
        "volume": 100.0 + rng.uniform(0, 20, count),
    })
    raw.loc[raw.index % 37 == 0, "volume"] *= 3.0

    one = strategy.populate_indicators(raw.copy(), {"pair": "BTC/USDT"})
    five_raw = (
        raw.set_index("date")
        .resample("5min")
        .agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
        .dropna()
        .reset_index()
    )
    five = strategy.populate_indicators_5m(
        five_raw.copy(), {"pair": "BTC/USDT"}
    )
    informative = five[["date", "ema9", "ema21", "ema50", "macdhist"]].copy()
    informative["date"] += pd.Timedelta(minutes=5)
    informative = informative.rename(columns={
        name: f"{name}_5m"
        for name in ["ema9", "ema21", "ema50", "macdhist"]
    })
    merged = pd.merge_asof(
        one.sort_values("date"),
        informative.sort_values("date"),
        on="date",
        direction="backward",
    )
    entered = strategy.populate_entry_trend(
        merged.copy(), {"pair": "BTC/USDT"}
    )
    negative_1m = merged.copy()
    negative_1m["macdhist"] = -999.0
    negative_1m = strategy.populate_entry_trend(
        negative_1m, {"pair": "BTC/USDT"}
    )
    positive_1m = merged.copy()
    positive_1m["macdhist"] = 999.0
    positive_1m = strategy.populate_entry_trend(
        positive_1m, {"pair": "BTC/USDT"}
    )
    negative_5m = merged.copy()
    negative_5m["macdhist_5m"] = -999.0
    negative_5m = strategy.populate_entry_trend(
        negative_5m, {"pair": "BTC/USDT"}
    )
    positive_5m = merged.copy()
    positive_5m["macdhist_5m"] = 999.0
    positive_5m = strategy.populate_entry_trend(
        positive_5m, {"pair": "BTC/USDT"}
    )

    def entries(frame):
        return int(frame.get("enter_long", pd.Series(dtype=float)).fillna(0).sum())

    prefix = strategy.populate_indicators(
        raw.iloc[:5000].copy(), {"pair": "BTC/USDT"}
    )
    max_diffs = {}
    for column in ["ema9", "ema21", "ema50", "rsi", "vwap", "rvol", "adx"]:
        left = one.loc[:4999, column].to_numpy(dtype=float)
        right = prefix[column].to_numpy(dtype=float)
        max_diffs[column] = float(np.nanmax(np.abs(left - right)))
    return {
        "candles": count,
        "entry_signals": entries(entered),
        "entries_macd_forced_negative": entries(negative_1m),
        "entries_macd_forced_positive": entries(positive_1m),
        "entries_macd5m_forced_negative": entries(negative_5m),
        "entries_macd5m_forced_positive": entries(positive_5m),
        "prefix_max_abs_diffs": max_diffs,
    }


def assert_strategy_smoke(result: dict) -> None:
    baseline = int(result["entry_signals"])
    positive_5m = int(result["entries_macd5m_forced_positive"])
    assert max(baseline, positive_5m) > 0, "strategy probe is vacuous"
    assert result["entries_macd5m_forced_negative"] == 0
    assert result["entries_macd_forced_negative"] == baseline
    assert result["entries_macd_forced_positive"] == baseline
    assert all(value < 1e-6 for value in result["prefix_max_abs_diffs"].values())


if __name__ == "__main__":
    output = strategy_smoke()
    assert_strategy_smoke(output)
    print(output)
