"""エントリー戦略: M15ブレイク + 上位足の方向一致 + ATR/出来高確認。

設計書 3.2 のロジックをそのまま実装。Strategy は「ある時点のトリガー足と
上位足の状態」を受け取り BUY / SELL / NONE のシグナルを返す純粋関数群。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

import pandas as pd

from . import analysis
from .analysis import MTFView, TREND_DOWN, TREND_UP
from .config import Settings

SIGNAL_BUY = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_NONE = "NONE"


@dataclass
class Signal:
    side: str  # BUY / SELL / NONE
    price: float = 0.0
    atr: float = 0.0
    reason: Dict[str, object] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.side in (SIGNAL_BUY, SIGNAL_SELL)


def _breakout(trigger_df: pd.DataFrame, lookback: int) -> Optional[str]:
    """最新バーの終値が直近 lookback 本（現在を除く）の高値/安値を抜けたか。

    ヒゲだけの抜けは終値判定により除外される。
    Returns: 'up' / 'down' / None
    """
    if len(trigger_df) < lookback + 1:
        return None
    window = trigger_df.iloc[-(lookback + 1):-1]
    last_close = trigger_df["close"].iloc[-1]
    prior_high = window["high"].max()
    prior_low = window["low"].min()
    if last_close > prior_high:
        return "up"
    if last_close < prior_low:
        return "down"
    return None


def _volume_increasing(trigger_df: pd.DataFrame, lookback: int) -> bool:
    """直近バーの出来高(tick volume)が過去平均より大きいか。"""
    if "volume" not in trigger_df.columns or len(trigger_df) < lookback + 1:
        return True  # 出来高情報が無ければこの条件は無効化（通す）
    recent = trigger_df["volume"].iloc[-(lookback + 1):-1].mean()
    if recent <= 0:
        return True
    return float(trigger_df["volume"].iloc[-1]) >= recent


def evaluate(
    trigger_df: pd.DataFrame,
    mtf: MTFView,
    settings: Settings,
) -> Signal:
    """トリガー足(指標付与済み) + 上位足ビューから売買シグナルを評価する。"""
    if trigger_df.empty:
        return Signal(SIGNAL_NONE)

    last = trigger_df.iloc[-1]
    atr_value = float(last.get("atr", 0.0) or 0.0)
    price = float(last["close"])

    # 1. 上位足の方向一致が無ければ何もしない
    if not mtf.is_aligned:
        return Signal(SIGNAL_NONE, price, atr_value, {"mtf": "not_aligned"})

    # 2. ブレイク方向
    brk = _breakout(trigger_df, settings.breakout_lookback)
    if brk is None or brk != mtf.aligned:
        return Signal(SIGNAL_NONE, price, atr_value,
                      {"breakout": brk, "mtf": mtf.aligned})

    # 3. ボラ確認: ATR の相対水準（過去比の百分位）
    atr_pct = _atr_percentile(trigger_df)
    if atr_pct is not None and atr_pct < settings.atr_min_pct:
        return Signal(SIGNAL_NONE, price, atr_value, {"atr_pct": atr_pct})

    # 4. 出来高確認
    if not _volume_increasing(trigger_df, settings.volume_lookback):
        return Signal(SIGNAL_NONE, price, atr_value, {"volume": "not_increasing"})

    reason = {
        "mtf": mtf.aligned,
        "mtf_states": mtf.states,
        "breakout": brk,
        "atr": atr_value,
        "atr_pct": atr_pct,
    }
    side = SIGNAL_BUY if mtf.aligned == TREND_UP else SIGNAL_SELL
    return Signal(side, price, atr_value, reason)


def _atr_percentile(trigger_df: pd.DataFrame, window: int = 100) -> Optional[float]:
    if "atr" not in trigger_df.columns:
        return None
    atr_series = trigger_df["atr"].dropna()
    if len(atr_series) < min(window, 20):
        return None
    window_series = atr_series.iloc[-window:]
    last = window_series.iloc[-1]
    return float((window_series <= last).mean())


def build_mtf(htf_frames: Dict[str, pd.DataFrame]) -> MTFView:
    """data_feed が返した上位足フレーム群から MTFView を構築する委譲関数。"""
    return analysis.evaluate_mtf(htf_frames)
