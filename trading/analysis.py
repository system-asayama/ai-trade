"""相場分析: 指標の付与・レンジ/トレンド判定・上位足の方向一致(MTF)。

すべて純粋関数。入力 DataFrame は ['open','high','low','close','volume'] を想定
（volume が無くても指標計算は動作する）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

from . import indicators
from .config import Settings

# トレンド状態の表現
TREND_UP = "up"
TREND_DOWN = "down"
TREND_RANGE = "range"

# レジーム
REGIME_TREND = "trend"
REGIME_RANGE = "range"
REGIME_TRANSITION = "transition"


def add_indicators(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """EMA / ATR / ADX とレジーム/トレンド状態を付与した DataFrame を返す。"""
    out = df.copy()
    out["ema_fast"] = indicators.ema(out["close"], settings.ema_fast)
    out["ema_mid"] = indicators.ema(out["close"], settings.ema_mid)
    out["ema_slow"] = indicators.ema(out["close"], settings.ema_slow)
    out["atr"] = indicators.atr(out, settings.atr_period)

    adx_df = indicators.adx(out, settings.adx_period)
    out["adx"] = adx_df["adx"]
    out["plus_di"] = adx_df["plus_di"]
    out["minus_di"] = adx_df["minus_di"]

    out["regime"] = _regime(out, settings)
    out["trend_state"] = _trend_state(out, settings)
    return out


def _regime(df: pd.DataFrame, settings: Settings) -> pd.Series:
    """ADX に基づくレジーム（trend / range / transition）。"""
    adx = df["adx"]
    regime = pd.Series(REGIME_TRANSITION, index=df.index, dtype="object")
    regime[adx >= settings.adx_trend_threshold] = REGIME_TREND
    regime[adx < settings.adx_range_threshold] = REGIME_RANGE
    return regime


def _trend_state(df: pd.DataFrame, settings: Settings) -> pd.Series:
    """EMA の並び＋ADX強度からトレンド方向（up / down / range）を判定。"""
    fast, mid, slow = df["ema_fast"], df["ema_mid"], df["ema_slow"]
    strong = df["adx"] >= settings.adx_trend_threshold

    up = (fast > mid) & (mid > slow) & strong
    down = (fast < mid) & (mid < slow) & strong

    state = pd.Series(TREND_RANGE, index=df.index, dtype="object")
    state[up] = TREND_UP
    state[down] = TREND_DOWN
    return state


def latest_trend_state(df: pd.DataFrame) -> str:
    """指標付与済み DataFrame の最新バーのトレンド状態を返す。"""
    if df.empty or "trend_state" not in df.columns:
        return TREND_RANGE
    value = df["trend_state"].iloc[-1]
    return value if isinstance(value, str) else TREND_RANGE


@dataclass
class MTFView:
    """上位足の方向一致の評価結果。"""

    states: Dict[str, str]  # granularity -> trend_state
    aligned: Optional[str]  # 'up' / 'down' / None（不一致）

    @property
    def is_aligned(self) -> bool:
        return self.aligned is not None


def evaluate_mtf(htf_frames: Dict[str, pd.DataFrame]) -> MTFView:
    """各上位足の最新トレンド状態を集計し、全一致なら方向を返す。

    htf_frames: {'H1': df, 'H4': df, 'D': df}（いずれも add_indicators 済み）
    """
    states = {gran: latest_trend_state(df) for gran, df in htf_frames.items()}
    values = set(states.values())
    if values == {TREND_UP}:
        aligned: Optional[str] = TREND_UP
    elif values == {TREND_DOWN}:
        aligned = TREND_DOWN
    else:
        aligned = None
    return MTFView(states=states, aligned=aligned)
