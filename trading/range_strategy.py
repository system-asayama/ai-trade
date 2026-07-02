"""ユーザー実手法のレンジ・ブレイク／押し目戻り戦略（M5ベース）。

ルール（ユーザー指定）:
- レンジ判定: 5分足で、ほぼ同じ価格を「片方の端3タッチ＋もう片方2タッチ」でレンジ確定。
- 上位足: 1時間足＆4時間足が同じ方向のときだけ（両モード共通）。
- モードA『順張り(breakout)』: 上位足方向にレンジを抜けた瞬間エントリー。
    損切り＝レンジ幅×1、利確＝レンジ幅×1（リスクリワード1:1）。
- モードB『押し目/戻り(pullback)』: 5分足では逆張り＝上位足は順張り。
    上昇なら下限タッチで買い／下降なら上限タッチで売り（タッチ即）。
    損切り＝レンジ幅×2、利確＝レンジ幅×2（1:1）。

決済は固定（トレーリングしない）。同一バーで損切り・利確の両方に触れた場合は
保守的に損切りを優先する。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from . import analysis
from .analysis import TREND_DOWN, TREND_UP
from .backtester import BacktestResult, BacktestTrade
from .config import Settings
from .data_feed import resample_ohlcv
from .strategy import SIGNAL_BUY, SIGNAL_SELL

MODE_BREAKOUT = "breakout"   # A: 順張りブレイク
MODE_PULLBACK = "pullback"   # B: 押し目/戻り（M5逆張り・上位足順張り）


def _htf_aligned_array(m5_df: pd.DataFrame, settings: Settings,
                       grans=("H1", "H4")) -> np.ndarray:
    """各M5バー時点で H1・H4 が同方向なら 'up'/'down'、そうでなければ None の配列。"""
    tindex = m5_df.index
    n = len(tindex)
    per = []
    for gran in grans:
        htf = analysis.add_indicators(resample_ohlcv(m5_df, gran), settings)
        arr = np.full(n, None, dtype=object)
        if len(htf):
            states = htf["trend_state"].to_numpy()
            pos = htf.index.searchsorted(tindex, side="right") - 1
            valid = pos >= 0
            arr[valid] = states[pos[valid]]
        per.append(arr)
    stacked = np.stack(per, axis=1)
    all_up = np.all(stacked == TREND_UP, axis=1)
    all_down = np.all(stacked == TREND_DOWN, axis=1)
    return np.where(all_up, TREND_UP, np.where(all_down, TREND_DOWN, None))


def run_range_strategy(
    instrument: str,
    m5_df: pd.DataFrame,
    settings: Optional[Settings] = None,
    mode: str = MODE_BREAKOUT,
    count_from: Optional[pd.Timestamp] = None,
    spread_pips: float = 0.8,
    slippage_pips: float = 0.2,
    window: int = 60,          # レンジ判定に使う直近M5本数（60本＝5時間）
    touch_tol_frac: float = 0.15,  # 端から何割以内をタッチとみなすか
    min_range_pips: float = 3.0,   # これ未満の極小レンジは無視（ノイズ）
) -> BacktestResult:
    """ユーザー手法のバックテストを実行し BacktestResult を返す。"""
    settings = settings or Settings()
    result = BacktestResult(instrument=instrument)
    pip = 0.01 if instrument.endswith("_JPY") else 0.0001
    cost = pip * (spread_pips / 2.0 + slippage_pips)
    min_range = min_range_pips * pip

    highs = m5_df["high"].to_numpy(dtype=float)
    lows = m5_df["low"].to_numpy(dtype=float)
    tindex = m5_df.index

    aligned = _htf_aligned_array(m5_df, settings)
    # 直前 window 本（現在バーを除く）のレンジ上限/下限
    top_arr = m5_df["high"].rolling(window).max().shift(1).to_numpy(dtype=float)
    bot_arr = m5_df["low"].rolling(window).min().shift(1).to_numpy(dtype=float)

    diag = {"bars": 0, "htf_aligned": 0, "range_ok": 0, "entries": 0}
    pos: Optional[BacktestTrade] = None

    def _open(side, entry_ref, stop, target, when, rng):
        entry = entry_ref + cost if side == SIGNAL_BUY else entry_ref - cost
        t = BacktestTrade(instrument=instrument, side=side, entry_time=when,
                          entry_price=entry, stop=stop,
                          initial_risk=abs(entry - stop),
                          entry_reason={"mode": mode, "range": round(rng, 5),
                                        "target": target})
        return t

    def _close(t, when, price, reason):
        if t.side == SIGNAL_BUY:
            exit_price = price - cost
            t.pnl_points = exit_price - t.entry_price
        else:
            exit_price = price + cost
            t.pnl_points = t.entry_price - exit_price
        t.exit_time = when
        t.exit_price = exit_price
        t.exit_reason = reason
        t.r_multiple = t.pnl_points / t.initial_risk if t.initial_risk > 0 else 0.0
        result.trades.append(t)

    start = max(window + 1, 2)
    for i in range(start, len(m5_df)):
        when = tindex[i]
        counting = count_from is None or when >= count_from
        hi, lo = highs[i], lows[i]

        # --- 既存ポジションの決済（固定・保守的に損切り優先） ---
        if pos is not None:
            tgt = pos.entry_reason.get("target")
            if pos.side == SIGNAL_BUY:
                if lo <= pos.stop:
                    _close(pos, when, pos.stop, "stop"); pos = None
                elif hi >= tgt:
                    _close(pos, when, tgt, "take_profit"); pos = None
            else:
                if hi >= pos.stop:
                    _close(pos, when, pos.stop, "stop"); pos = None
                elif lo <= tgt:
                    _close(pos, when, tgt, "take_profit"); pos = None

        if counting:
            diag["bars"] += 1
        if pos is not None:
            continue

        trend = aligned[i]
        if trend is None:
            continue
        if counting:
            diag["htf_aligned"] += 1

        top, bot = top_arr[i], bot_arr[i]
        if not (np.isfinite(top) and np.isfinite(bot)):
            continue
        rng = top - bot
        if rng < min_range:
            continue

        # タッチ回数（片方3・もう片方2）の確認
        w_hi = highs[i - window:i]
        w_lo = lows[i - window:i]
        tol = touch_tol_frac * rng
        tt = int(np.count_nonzero(w_hi >= top - tol))
        tb = int(np.count_nonzero(w_lo <= bot + tol))
        if not ((tt >= 3 and tb >= 2) or (tt >= 2 and tb >= 3)):
            continue
        if counting:
            diag["range_ok"] += 1

        # --- エントリー判定 ---
        if mode == MODE_BREAKOUT:
            if trend == TREND_UP and hi >= top:
                pos = _open(SIGNAL_BUY, top, top - rng, top + rng, when, rng)
            elif trend == TREND_DOWN and lo <= bot:
                pos = _open(SIGNAL_SELL, bot, bot + rng, bot - rng, when, rng)
        else:  # MODE_PULLBACK（上位足順張り・M5逆張り）
            if trend == TREND_UP and lo <= bot:
                pos = _open(SIGNAL_BUY, bot, bot - 2 * rng, bot + 2 * rng, when, rng)
            elif trend == TREND_DOWN and hi >= top:
                pos = _open(SIGNAL_SELL, top, top + 2 * rng, top - 2 * rng, when, rng)

        if pos is not None and counting:
            diag["entries"] += 1

    if pos is not None:
        _close(pos, tindex[-1], float(m5_df["close"].iloc[-1]), "end_of_data")

    if count_from is not None:
        result.trades = [t for t in result.trades if t.entry_time >= count_from]
    result.diagnostics = diag
    return result
