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


def _breakout_bar_quality(bar, direction: str):
    """ブレイク足の (実体比, 終値の位置) を返す。

    実体比 = |終値-始値| / (高値-安値)。1に近いほど力強い（ヒゲが少ない）。
    終値の位置 = 抜けた方向の端に終値がどれだけ寄っているか（1で端、0で反対端）。
    どちらも大きいほど「素直に伸びやすい強いブレイク」。
    """
    o = float(bar.get("open", 0.0) or 0.0)
    h = float(bar.get("high", 0.0) or 0.0)
    low = float(bar.get("low", 0.0) or 0.0)
    c = float(bar.get("close", 0.0) or 0.0)
    rng = h - low
    if rng <= 0:
        return 0.0, 0.5
    body_frac = abs(c - o) / rng
    close_pos = (c - low) / rng  # 上端に近いほど大
    if direction == "down":
        close_pos = 1.0 - close_pos  # 下抜けは下端に近いほど良い
    return body_frac, close_pos


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
    fakeout_model: object = None,
) -> Signal:
    """トリガー足(指標付与済み) + 上位足ビューから売買シグナルを評価する。

    fakeout_model を渡すと、全条件を満たした後に「ダマシ確率」で最終フィルタする
    （成功確率が settings.fakeout_min_proba 未満なら見送り）。未学習/未指定なら無視。
    """
    if trigger_df.empty:
        return Signal(SIGNAL_NONE)

    last = trigger_df.iloc[-1]
    atr_value = float(last.get("atr", 0.0) or 0.0)
    adx_value = float(last.get("adx", 0.0) or 0.0)
    price = float(last["close"])

    # 1. 上位足の方向一致が無ければ何もしない
    if not mtf.is_aligned:
        return Signal(SIGNAL_NONE, price, atr_value, {"stage": "mtf", "mtf": "not_aligned"})

    # 2. ブレイク方向
    brk = _breakout(trigger_df, settings.breakout_lookback)
    if brk is None or brk != mtf.aligned:
        return Signal(SIGNAL_NONE, price, atr_value,
                      {"stage": "breakout", "breakout": brk, "mtf": mtf.aligned})

    # 2b. ブレイク足の質（強いブレイクのみ）。実体が薄い/終値が端に無いブレイクは
    #     「抜けた直後に逆行」しやすいダマシなので除外する（プライスアクション）。
    if settings.breakout_body_min > 0:
        body_frac, close_pos = _breakout_bar_quality(last, brk)
        if body_frac < settings.breakout_body_min or close_pos < 0.6:
            return Signal(SIGNAL_NONE, price, atr_value,
                          {"stage": "weakbreak", "body_frac": round(body_frac, 2),
                           "close_pos": round(close_pos, 2)})

    # 3. ボラ確認: ATR の相対水準（過去比の百分位）
    atr_pct = _atr_percentile(trigger_df)
    if atr_pct is not None and atr_pct < settings.atr_min_pct:
        return Signal(SIGNAL_NONE, price, atr_value, {"stage": "atr", "atr_pct": atr_pct})

    # 4. 出来高確認
    if not _volume_increasing(trigger_df, settings.volume_lookback):
        return Signal(SIGNAL_NONE, price, atr_value,
                      {"stage": "volume", "volume": "not_increasing"})

    # 5. レンジ回避: トリガー足のADX（トレンド強度）が弱すぎるなら見送り
    if settings.entry_adx_min > 0 and adx_value < settings.entry_adx_min:
        return Signal(SIGNAL_NONE, price, atr_value,
                      {"stage": "regime", "regime_adx": adx_value, "min": settings.entry_adx_min})

    reason = {
        "stage": "entry",
        "mtf": mtf.aligned,
        "mtf_states": mtf.states,
        "breakout": brk,
        "atr": atr_value,
        "atr_pct": atr_pct,
        "adx": adx_value,
        "volume_ratio": _volume_ratio(trigger_df, settings.volume_lookback),
    }

    # 6. ダマシ予測ML（学習済みモデルがあれば最終フィルタ）
    if fakeout_model is not None and getattr(fakeout_model, "is_trained", False):
        from .ml import features_from_reason
        proba = fakeout_model.predict_proba(features_from_reason(reason))
        reason["fakeout_proba"] = round(float(proba), 4)
        if proba < settings.fakeout_min_proba:
            reason = dict(reason)
            reason["stage"] = "fakeout"
            return Signal(SIGNAL_NONE, price, atr_value, reason)

    side = SIGNAL_BUY if mtf.aligned == TREND_UP else SIGNAL_SELL
    return Signal(side, price, atr_value, reason)


def _volume_ratio(trigger_df: pd.DataFrame, lookback: int) -> float:
    """直近バーの出来高が過去平均の何倍か（情報が無ければ 1.0）。"""
    if "volume" not in trigger_df.columns or len(trigger_df) < lookback + 1:
        return 1.0
    recent = trigger_df["volume"].iloc[-(lookback + 1):-1].mean()
    if recent <= 0:
        return 1.0
    return float(trigger_df["volume"].iloc[-1]) / recent


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
