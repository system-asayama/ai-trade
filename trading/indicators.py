"""テクニカル指標（pandas / numpy で自前実装）。

外部の TA ライブラリに依存しないことで、インストールの安定性と
テストの決定性を確保する。すべて Wilder の平滑化（RMA）を採用。

入力 DataFrame は最低限 ['high', 'low', 'close'] 列を持つ前提。
"""
from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """指数移動平均（EMA）。"""
    return series.ewm(span=period, adjust=False).mean()


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder の平滑移動平均（RMA = ewm(alpha=1/period)）。"""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range。"""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range（Wilder 平滑）。"""
    return rma(true_range(df), period)


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX / +DI / -DI を返す。

    Returns: 列 ['adx', 'plus_di', 'minus_di'] を持つ DataFrame。
    """
    high = df["high"]
    low = df["low"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = true_range(df)
    atr_ = rma(tr, period)

    # 0除算を避ける
    plus_di = 100.0 * rma(plus_dm, period) / atr_.replace(0.0, pd.NA)
    minus_di = 100.0 * rma(minus_dm, period) / atr_.replace(0.0, pd.NA)

    di_sum = (plus_di + minus_di).replace(0.0, pd.NA)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx_ = rma(dx.fillna(0.0), period)

    return pd.DataFrame(
        {
            "adx": adx_,
            "plus_di": plus_di.fillna(0.0),
            "minus_di": minus_di.fillna(0.0),
        }
    )


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """直近 window 内での各値の百分位（0..1）。

    現在値が過去ウィンドウ内で相対的に高い/低いかを 0..1 で表す。
    """

    def _pct(x: pd.Series) -> float:
        last = x.iloc[-1]
        return float((x <= last).mean())

    return series.rolling(window, min_periods=window).apply(_pct, raw=False)
