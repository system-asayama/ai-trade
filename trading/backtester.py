"""イベントドリブンのバックテスト基盤（Phase 1）。

トリガー足(M15)の各バーを時系列に進め、上位足は同一データから
リサンプルして「その時点までに確定した足」の状態だけを参照する
（ルックアヘッド・バイアスを避ける）。

損益はインストルメント非依存の **R倍数**（初期リスク幅で正規化）を主指標とし、
価格ポイントの損益も併記する。実運用のロット/通貨換算は Phase 2 で扱う。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from . import analysis, strategy
from .analysis import MTFView, TREND_RANGE
from .config import Settings
from .data_feed import resample_ohlcv
from .strategy import SIGNAL_BUY, SIGNAL_SELL


@dataclass
class BacktestTrade:
    instrument: str
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    stop: float
    initial_risk: float = 0.0  # エントリー時の初期リスク幅（R正規化の基準）
    exit_time: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    r_multiple: float = 0.0
    pnl_points: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.exit_time is None


@dataclass
class BacktestResult:
    instrument: str
    trades: List[BacktestTrade] = field(default_factory=list)

    @property
    def closed(self) -> List[BacktestTrade]:
        return [t for t in self.trades if not t.is_open]

    @property
    def num_trades(self) -> int:
        return len(self.closed)

    @property
    def win_rate(self) -> float:
        if not self.closed:
            return 0.0
        wins = sum(1 for t in self.closed if t.r_multiple > 0)
        return wins / len(self.closed)

    @property
    def total_r(self) -> float:
        return sum(t.r_multiple for t in self.closed)

    @property
    def expectancy_r(self) -> float:
        return self.total_r / len(self.closed) if self.closed else 0.0

    @property
    def max_drawdown_r(self) -> float:
        """Rベースのエクイティ曲線の最大ドローダウン。"""
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.closed:
            equity += t.r_multiple
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return max_dd

    def summary(self) -> Dict[str, object]:
        return {
            "instrument": self.instrument,
            "num_trades": self.num_trades,
            "win_rate": round(self.win_rate, 4),
            "total_r": round(self.total_r, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "max_drawdown_r": round(self.max_drawdown_r, 4),
        }


class Backtester:
    """単一インストルメント・単一ポジションのバックテスター。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _htf_states_at(
        self, htf_indicators: Dict[str, pd.DataFrame], when: pd.Timestamp
    ) -> MTFView:
        """各上位足について when までに確定した最新バーのトレンド状態を取る。"""
        states: Dict[str, str] = {}
        for gran, df in htf_indicators.items():
            # when 以下の最後の行を取得
            pos = df.index.searchsorted(when, side="right") - 1
            if pos < 0:
                states[gran] = TREND_RANGE
            else:
                value = df["trend_state"].iloc[pos]
                states[gran] = value if isinstance(value, str) else TREND_RANGE
        values = set(states.values())
        from .analysis import TREND_DOWN, TREND_UP

        if values == {TREND_UP}:
            aligned: Optional[str] = TREND_UP
        elif values == {TREND_DOWN}:
            aligned = TREND_DOWN
        else:
            aligned = None
        return MTFView(states=states, aligned=aligned)

    def run(self, instrument: str, trigger_df: pd.DataFrame) -> BacktestResult:
        """トリガー足(生OHLCV)を与えてバックテストを実行する。

        上位足は trigger_df から settings.htf_granularities へリサンプルする。
        """
        settings = self.settings
        result = BacktestResult(instrument=instrument)

        trigger = analysis.add_indicators(trigger_df, settings)

        htf_indicators: Dict[str, pd.DataFrame] = {}
        for gran in settings.htf_granularities:
            resampled = resample_ohlcv(trigger_df, gran)
            htf_indicators[gran] = analysis.add_indicators(resampled, settings)

        # 指標が安定するまでのウォームアップ
        warmup = max(settings.ema_slow, settings.breakout_lookback + 1)
        position: Optional[BacktestTrade] = None

        for i in range(warmup, len(trigger)):
            bar = trigger.iloc[i]
            when = trigger.index[i]

            # --- 既存ポジションの管理（当該バーで先に決済判定） ---
            if position is not None:
                position = self._manage_position(position, bar, when, result)

            # --- 反対シグナル/新規エントリーの評価 ---
            slice_df = trigger.iloc[: i + 1]
            mtf = self._htf_states_at(htf_indicators, when)
            signal = strategy.evaluate(slice_df, mtf, settings)

            if position is not None:
                # 反対シグナルが出たらクローズ
                if signal.is_entry and signal.side != position.side:
                    self._close(position, when, float(bar["close"]),
                                "opposite_signal", result)
                    position = None

            if position is None and signal.is_entry:
                position = self._open(instrument, signal, bar, when)

        # 最終バーで残ポジは終値クローズ
        if position is not None:
            last_bar = trigger.iloc[-1]
            self._close(position, trigger.index[-1], float(last_bar["close"]),
                        "end_of_data", result)

        return result

    # -- ポジション操作 ------------------------------------------------------
    def _open(self, instrument: str, signal, bar, when) -> BacktestTrade:
        entry = signal.price
        atr_value = signal.atr or float(bar.get("atr", 0.0) or 0.0)
        dist = self.settings.atr_stop_mult * atr_value
        if signal.side == SIGNAL_BUY:
            stop = entry - dist
        else:
            stop = entry + dist
        return BacktestTrade(
            instrument=instrument,
            side=signal.side,
            entry_time=when,
            entry_price=entry,
            stop=stop,
            initial_risk=abs(entry - stop),
        )

    def _manage_position(self, pos: BacktestTrade, bar, when, result) -> Optional[BacktestTrade]:
        """ストップ判定 → トレーリング更新。決済したら None を返す。"""
        high = float(bar["high"])
        low = float(bar["low"])
        atr_value = float(bar.get("atr", 0.0) or 0.0)

        if pos.side == SIGNAL_BUY:
            if low <= pos.stop:  # ストップ約定
                self._close(pos, when, pos.stop, "stop", result)
                return None
            # トレーリング（建値方向にのみ引き上げ）
            new_stop = bar["close"] - self.settings.atr_trail_mult * atr_value
            pos.stop = max(pos.stop, float(new_stop))
        else:  # SELL
            if high >= pos.stop:
                self._close(pos, when, pos.stop, "stop", result)
                return None
            new_stop = bar["close"] + self.settings.atr_trail_mult * atr_value
            pos.stop = min(pos.stop, float(new_stop))
        return pos

    def _close(self, pos: BacktestTrade, when, price, reason, result: BacktestResult) -> None:
        pos.exit_time = when
        pos.exit_price = price
        pos.exit_reason = reason
        if pos.side == SIGNAL_BUY:
            pos.pnl_points = price - pos.entry_price
        else:
            pos.pnl_points = pos.entry_price - price
        # R倍数はエントリー時の初期リスク幅で正規化（トレーリング後のストップではない）
        risk = pos.initial_risk
        pos.r_multiple = pos.pnl_points / risk if risk > 0 else 0.0
        result.trades.append(pos)
