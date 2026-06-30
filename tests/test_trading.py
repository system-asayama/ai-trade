"""トレーディングエンジン Phase 1 のテスト。

pytest があればそのまま、無ければ `python tests/test_trading.py` でも実行可能。
ネットワーク非依存（合成データを使用）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading import analysis, indicators, strategy  # noqa: E402
from trading.analysis import TREND_DOWN, TREND_UP  # noqa: E402
from trading.backtester import Backtester  # noqa: E402
from trading.config import Settings  # noqa: E402
from trading.data_feed import candles_to_df, resample_ohlcv  # noqa: E402
from trading.synthetic import make_ohlcv  # noqa: E402


def _settings() -> Settings:
    # 合成データに合わせて EMA slow を短く（200本も待たない）
    os.environ.setdefault("EMA_SLOW", "100")
    return Settings()


def test_atr_positive_and_finite():
    df = make_ohlcv(500)
    atr = indicators.atr(df, 14).dropna()
    assert (atr > 0).all()
    assert np.isfinite(atr.to_numpy()).all()


def test_adx_columns_and_range():
    df = make_ohlcv(500)
    adx_df = indicators.adx(df, 14).dropna()
    assert set(adx_df.columns) == {"adx", "plus_di", "minus_di"}
    # ADX / DI は 0..100 の範囲
    assert (adx_df["adx"] >= 0).all() and (adx_df["adx"] <= 100).all()
    assert (adx_df["plus_di"] >= 0).all()


def test_ema_tracks_uptrend():
    # 単調増加なら EMA は終値より遅れて下に位置する
    s = pd.Series(np.arange(1, 200, dtype=float))
    e = indicators.ema(s, 20)
    assert e.iloc[-1] < s.iloc[-1]
    assert e.iloc[-1] > e.iloc[0]


def test_trend_state_detects_uptrend():
    settings = _settings()
    # 強い上昇トレンドを作る
    n = 400
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    close = np.linspace(100, 140, n)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )
    out = analysis.add_indicators(df, settings)
    assert analysis.latest_trend_state(out) == TREND_UP


def test_candles_to_df_skips_incomplete():
    candles = [
        {"time": "2024-01-01T00:00:00Z", "complete": True,
         "mid": {"o": "1.0", "h": "1.2", "l": "0.9", "c": "1.1"}, "volume": 10},
        {"time": "2024-01-01T00:15:00Z", "complete": False,
         "mid": {"o": "1.1", "h": "1.3", "l": "1.0", "c": "1.2"}, "volume": 5},
    ]
    df = candles_to_df(candles)
    assert len(df) == 1
    assert df["close"].iloc[0] == 1.1


def test_resample_m15_to_h1():
    df = make_ohlcv(400, granularity_minutes=15)
    h1 = resample_ohlcv(df, "H1")
    # 4本のM15が1本のH1に集約される（端数を許容）
    assert len(h1) <= len(df) // 4 + 1
    assert {"open", "high", "low", "close", "volume"}.issubset(h1.columns)


def test_mtf_alignment():
    settings = _settings()
    up = analysis.add_indicators(_linear_df(100, 140), settings)
    states = {"H1": up, "H4": up, "D": up}
    view = analysis.evaluate_mtf(states)
    assert view.aligned == TREND_UP
    assert view.is_aligned

    down = analysis.add_indicators(_linear_df(140, 100), settings)
    mixed = analysis.evaluate_mtf({"H1": up, "H4": down, "D": up})
    assert not mixed.is_aligned


def test_backtest_runs_and_produces_trades():
    settings = _settings()
    df = make_ohlcv(3000)
    bt = Backtester(settings)
    result = bt.run("USD_JPY", df)
    summary = result.summary()
    # 何らかのトレードが発生し、サマリが計算できること
    assert summary["num_trades"] >= 1
    assert -1.0 <= summary["win_rate"] <= 1.0
    # R倍数は有限
    assert np.isfinite(result.total_r)
    # ストップは常に「約定価格＝ストップ値」で執行されるため、
    # トレーリングで引き上げた後でも損失は初期リスク(-1R)を下回らない。
    for t in result.closed:
        if t.exit_reason == "stop":
            assert t.r_multiple >= -1.0 - 1e-9


def _linear_df(start: float, end: float, n: int = 300) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    close = np.linspace(start, end, n)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
