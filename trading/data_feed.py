"""データフィード: OANDA の生ローソク足を pandas DataFrame に正規化する。

ネットワークに依存しない正規化関数（candles_to_df / resample_ohlcv）を分離し、
テストや バックテスト で再利用できるようにする。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .config import Settings
from .oanda_client import OandaClient

# OANDA granularity -> pandas resample rule
_RESAMPLE_RULE = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D": "1D",
}


def candles_to_df(candles: List[Dict[str, Any]], price: str = "mid") -> pd.DataFrame:
    """OANDA の candle リストを OHLCV DataFrame に変換する。

    index は UTC の DatetimeIndex。未確定足(complete=False)は除外する。
    price: 'mid' / 'bid' / 'ask'（candle 内のキー mid/bid/ask に対応）。
    """
    times: List[Any] = []
    opens: List[Any] = []
    highs: List[Any] = []
    lows: List[Any] = []
    closes: List[Any] = []
    vols: List[Any] = []
    for c in candles:
        if not c.get("complete", False):
            continue
        ohlc = c.get(price) or c.get("mid")
        if ohlc is None:
            continue
        times.append(c["time"])
        opens.append(ohlc["o"])
        highs.append(ohlc["h"])
        lows.append(ohlc["l"])
        closes.append(ohlc["c"])
        vols.append(c.get("volume", 0))
    if not times:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    # 時刻は1本ずつ pd.to_datetime せず、まとめて一括変換（長期データで桁違いに速い）
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "open": np.asarray(opens, dtype=float),
            "high": np.asarray(highs, dtype=float),
            "low": np.asarray(lows, dtype=float),
            "close": np.asarray(closes, dtype=float),
            "volume": np.asarray(vols, dtype=float),
        }
    ).set_index("time").sort_index()
    return df


def resample_ohlcv(df: pd.DataFrame, granularity: str) -> pd.DataFrame:
    """下位足の OHLCV をより上位の足へリサンプルする（バックテスト用）。"""
    rule = _RESAMPLE_RULE.get(granularity)
    if rule is None:
        raise ValueError(f"未対応の granularity: {granularity}")
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = df.resample(rule, label="right", closed="right").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])


class DataFeed:
    """OANDA からローソク足を取得して DataFrame で返す。"""

    def __init__(self, settings: Settings, client: Optional[OandaClient] = None) -> None:
        self.settings = settings
        self.client = client or OandaClient(settings)

    def fetch(self, instrument: str, granularity: str, count: int = 500) -> pd.DataFrame:
        candles = self.client.get_candles(instrument, granularity, count=count)
        return candles_to_df(candles)

    def fetch_multi_timeframe(
        self, instrument: str, count: int = 500
    ) -> Dict[str, pd.DataFrame]:
        """トリガー足＋上位足をまとめて取得する。"""
        frames: Dict[str, pd.DataFrame] = {}
        for gran in [self.settings.trigger_granularity, *self.settings.htf_granularities]:
            frames[gran] = self.fetch(instrument, gran, count=count)
        return frames
