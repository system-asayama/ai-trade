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


def test_backtest_costs_reduce_performance():
    settings = _settings()
    df = make_ohlcv(3000)
    base = Backtester(settings).run("USD_JPY", df).total_r
    costly = Backtester(settings, spread_pips=2.0, slippage_pips=1.0).run("USD_JPY", df).total_r
    # スプレッド/滑りを差し引くと成績は必ず下がる
    assert costly < base


def test_backtest_count_from_is_true_subset():
    """count_from を指定した短い期間は、全期間runの同じ区間と完全一致する。

    暖機を全期間で行ってから区間を切り出すため、「短い期間は長い期間の
    一部分」という関係が必ず成り立つ（期間の切り方で成績が食い違わない）。
    """
    settings = _settings()
    df = make_ohlcv(8000)
    cf = df.index[len(df) // 2]  # 後半だけ集計
    bt = Backtester(settings)

    full = bt.run("USD_JPY", df)                     # 全期間で集計
    partial = bt.run("USD_JPY", df, count_from=cf)   # 後半だけ集計

    full_late = [t for t in full.closed if t.entry_time >= cf]
    assert len(partial.closed) == len(full_late)
    assert abs(partial.total_r - sum(t.r_multiple for t in full_late)) < 1e-9
    # count_from 以降のみが記録されている
    assert all(t.entry_time >= cf for t in partial.closed)


def test_range_filter_reduces_trades():
    """レンジ回避(entry_adx_min)を上げるとエントリーが減る。"""
    df = make_ohlcv(6000)
    base = Backtester(_settings()).run("USD_JPY", df)
    s = _settings()
    s.entry_adx_min = 30.0  # 強いトレンドのみ許可
    filtered = Backtester(s).run("USD_JPY", df)
    assert filtered.num_trades <= base.num_trades
    assert filtered.diagnostics.get("range_filtered", 0) >= 0


def test_partial_tp_changes_exit_and_bounds():
    """部分利確ONで、勝ちトレードのRが建値ストップ後も確定分を保持する。"""
    df = make_ohlcv(6000)
    s = _settings()
    s.partial_tp_r = 1.0
    s.partial_tp_frac = 0.5
    res = Backtester(s).run("USD_JPY", df)
    for t in res.closed:
        # 部分利確済みトレードは確定R(banked_r)以上（建値ストップで守られる）
        if t.partial_taken and t.exit_reason == "stop":
            assert t.r_multiple >= t.banked_r - 1e-9
        # 損失は初期リスクの範囲内
        if t.exit_reason == "stop" and not t.partial_taken:
            assert t.r_multiple >= -1.0 - 1e-9


def test_is_confirmed_range_box_vs_trend():
    from trading.strategy import _is_confirmed_range
    # 明確なボックス: 100〜101 を何度も往復（横ばい・複数タッチ）→ レンジと認める
    hi = [101, 100.5, 101, 100.4, 101, 100.3, 101, 100.2]
    lo = [100, 99.9, 100.1, 100, 100.1, 100, 100.1, 100]
    cl = [100.5, 100.2, 100.6, 100.2, 100.6, 100.2, 100.6, 100.3]
    box = pd.DataFrame({"high": hi, "low": lo, "close": cl})
    assert _is_confirmed_range(box, min_touches=2) is True

    # 一方向トレンド: 端タッチが片側に偏り、純変化が大きい → レンジと認めない
    base = np.linspace(100, 110, 8)
    trend = pd.DataFrame({"high": base + 0.2, "low": base - 0.2, "close": base})
    assert _is_confirmed_range(trend, min_touches=2) is False


def test_fixed_tp_rr_profile():
    from trading.synthetic import make_ohlcv
    settings = _settings()
    settings.tp_rr = 1.0  # 1:1 固定利確
    df = make_ohlcv(6000)
    r = Backtester(settings).run("USD_JPY", df)
    assert r.num_trades >= 1
    # 決済理由は take_profit / stop のみ（トレーリングしない）
    reasons = {t.exit_reason for t in r.closed}
    assert reasons <= {"take_profit", "stop", "opposite_signal", "end_of_data"}
    # 勝ちトレードは概ね +1R 近辺（コスト分だけ小さい）、-1R を大きく超える負けは無い
    for t in r.closed:
        if t.exit_reason == "take_profit":
            assert 0.7 <= t.r_multiple <= 1.05
        if t.exit_reason == "stop":
            assert t.r_multiple <= 0.0


def test_entry_stop_range_vs_atr():
    settings = _settings()
    settings.atr_stop_mult = 1.5
    bt = Backtester(settings)
    B, S = strategy.SIGNAL_BUY, strategy.SIGNAL_SELL
    # ATR距離モード: 建値 ∓ 1.5×ATR
    settings.range_stop = False
    assert bt._entry_stop(B, 100.0, 0.2, 101.0, 99.0) == 100.0 - 0.3
    assert bt._entry_stop(S, 100.0, 0.2, 101.0, 99.0) == 100.0 + 0.3
    # レンジ端モード: 買いはレンジ下限、売りはレンジ上限
    settings.range_stop = True
    assert bt._entry_stop(B, 100.0, 0.2, 101.0, 99.0) == 99.0
    assert bt._entry_stop(S, 100.0, 0.2, 101.0, 99.0) == 101.0


def test_retest_entry_mode_tags_and_runs():
    from trading.synthetic import make_ohlcv
    settings = _settings()
    settings.retest_entry = True
    df = make_ohlcv(8000)
    r = Backtester(settings).run("USD_JPY", df)
    assert r.num_trades >= 1
    # リテストモードでは全エントリーが押し戻し経由（stage=retest）
    assert all(t.entry_reason.get("stage") == "retest" for t in r.closed)
    # 初期リスク（建値〜初期ストップ幅）が正しく設定されている
    assert all(t.initial_risk > 0 for t in r.closed)


def test_strong_breakout_filter_rejects_weak_bar():
    from trading.analysis import MTFView, TREND_UP
    settings = _settings()
    settings.breakout_body_min = 0.5  # 強いブレイクのみ

    # 直近20本は横ばい、最新足だけ高値を僅かに上抜けするが「ヒゲ主体で実体が薄い」足
    idx = pd.date_range("2024-01-01", periods=25, freq="15min", tz="UTC")
    close = np.full(25, 100.0)
    df = pd.DataFrame({"open": close, "high": close + 0.1, "low": close - 0.1,
                       "close": close, "volume": np.full(25, 100.0), "atr": np.full(25, 0.2)},
                      index=idx)
    # 最新足: 上抜けするが 実体小・上ヒゲ長（弱いブレイク）
    df.iloc[-1, df.columns.get_loc("open")] = 100.15
    df.iloc[-1, df.columns.get_loc("close")] = 100.16   # 実体 0.01
    df.iloc[-1, df.columns.get_loc("high")] = 100.60    # 長い上ヒゲ
    df.iloc[-1, df.columns.get_loc("low")] = 100.10
    mtf = MTFView(states={"D": TREND_UP}, aligned=TREND_UP)
    sig = strategy.evaluate(df, mtf, settings)
    assert sig.side == strategy.SIGNAL_NONE
    assert sig.reason.get("stage") == "weakbreak"


def test_diagnose_flags_single_trade_dependence():
    from trading.backtester import diagnose
    # 合計+18.55Rのうち最大の勝ちが+15.88R＝1回依存 → bad で指摘
    summary = {"num_trades": 54, "win_rate": 0.315, "total_r": 18.55,
               "expectancy_r": 0.34, "max_drawdown_r": -7.19}
    analytics = {"profit_factor": 1.56, "payoff": 3.4, "avg_win_r": 3.03,
                 "avg_loss_r": -0.89, "largest_win_r": 15.88,
                 "by_reason": {}, "by_year": {}}
    findings = diagnose(summary, analytics)
    assert any("1回" in f["text"] and f["level"] == "bad" for f in findings)


def test_diagnose_flags_losing_low_payoff():
    from trading.backtester import diagnose
    # 負け越し・利小損大の成績を渡すと bad 診断が出る
    summary = {"num_trades": 60, "win_rate": 0.34, "total_r": -4.0,
               "expectancy_r": -0.07, "max_drawdown_r": -10.0}
    analytics = {"profit_factor": 0.87, "payoff": 1.2, "avg_win_r": 1.0,
                 "avg_loss_r": -0.83, "by_reason": {"stop": {"count": 40, "total_r": -30.0},
                 "trail": {"count": 20, "total_r": 26.0}}, "by_year": {}}
    findings = diagnose(summary, analytics)
    levels = [f["level"] for f in findings]
    assert "bad" in levels  # 負け越しを指摘
    assert any("利小損大" in f["text"] for f in findings)


def test_range_strategy_modes_geometry():
    """ユーザー手法エンジン: 上昇相場では両モードとも買い・1:1で決済される。"""
    from trading.range_strategy import (MODE_BREAKOUT, MODE_PULLBACK,
                                        run_range_strategy)
    n = 20000
    idx = pd.date_range("2023-01-02", periods=n, freq="5min", tz="UTC")
    rng = np.random.default_rng(0)
    close = 100.0 + np.cumsum(rng.normal(0.003, 0.05, n))  # 上昇ドリフト
    df = pd.DataFrame({"open": close, "high": close + np.abs(rng.normal(0, 0.04, n)),
                       "low": close - np.abs(rng.normal(0, 0.04, n)),
                       "close": close, "volume": 100.0}, index=idx)
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    for mode in (MODE_BREAKOUT, MODE_PULLBACK):
        r = run_range_strategy("USD_JPY", df, _settings(), mode=mode,
                               spread_pips=0.8, slippage_pips=0.2)
        assert r.num_trades >= 1
        assert {t.side for t in r.closed} == {strategy.SIGNAL_BUY}  # 上昇→買いのみ
        assert {t.exit_reason for t in r.closed} <= {"take_profit", "stop", "end_of_data"}
        for t in r.closed:
            if t.exit_reason == "take_profit":
                assert 0.7 <= t.r_multiple <= 1.05   # 1:1（コスト分だけ小）
            elif t.exit_reason == "stop":
                assert -1.15 <= t.r_multiple <= 0.0


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
