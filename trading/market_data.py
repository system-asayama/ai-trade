"""無料・登録不要のリアル価格データ取得（ペーパートレード用）。

Yahoo Finance の公開チャートJSON（APIキー不要）から為替レートを取得し、
OANDA 互換のローソク足に変換する。本人確認や口座は不要（データ閲覧のみ）。

requests を注入可能にし、ネットワーク非依存でテストできる。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

import pandas as pd

from .data_feed import candles_to_df, resample_ohlcv

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_UA = "Mozilla/5.0 (compatible; ai-trade-paper/1.0)"

# OANDA granularity -> (Yahoo interval, range, 追加リサンプル先)
_INTERVAL = {
    "M1": ("1m", "5d", None),
    "M5": ("5m", "1mo", None),
    "M15": ("15m", "1mo", None),
    "M30": ("30m", "1mo", None),
    "H1": ("60m", "3mo", None),
    "H4": ("60m", "3mo", "H4"),   # 4h は 60m を集約
    "D": ("1d", "1y", None),
}


def to_symbol(instrument: str) -> str:
    """'USD_JPY' -> 'USDJPY=X'（Yahoo の為替シンボル）。"""
    return instrument.replace("_", "") + "=X"


class YahooMarketData:
    def __init__(self, session: Optional[Any] = None) -> None:
        if session is not None:
            self._session = session
        elif requests is not None:
            self._session = requests.Session()
        else:  # pragma: no cover
            self._session = None

    def get_candles(self, instrument: str, granularity: str,
                    count: int = 500, price: str = "M") -> List[Dict[str, Any]]:
        if self._session is None:  # pragma: no cover
            raise RuntimeError("requests が利用できません。")
        interval, rng, resample_to = _INTERVAL.get(granularity, ("15m", "1mo", None))
        url = _CHART_URL.format(symbol=to_symbol(instrument))
        resp = self._session.get(url, params={"interval": interval, "range": rng},
                                 headers={"User-Agent": _UA}, timeout=30)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        candles = _parse_yahoo(resp.json())
        if resample_to:
            candles = _resample_candles(candles, resample_to)
        return candles[-count:] if count else candles

    def get_pricing(self, instruments: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for inst in instruments:
            try:
                candles = self.get_candles(inst, "M15", count=1)
                mid = candles[-1]["mid"]["c"] if candles else 0.0
            except Exception:  # noqa: BLE001
                mid = 0.0
            out[inst] = {"bids": [{"price": mid}], "asks": [{"price": mid}]}
        return out


def _parse_yahoo(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Yahoo チャートJSON を OANDA 互換のローソク足へ変換する。"""
    chart = (data or {}).get("chart", {})
    results = chart.get("result") or []
    if not results:
        return []
    res = results[0]
    timestamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []

    out = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = (_at(opens, i), _at(highs, i), _at(lows, i), _at(closes, i))
        if None in (o, h, l, c):
            continue  # 欠損足はスキップ
        t = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        out.append({
            "time": t, "complete": True,
            "mid": {"o": float(o), "h": float(h), "l": float(l), "c": float(c)},
            "volume": _at(vols, i) or 0,
        })
    return out


def _resample_candles(candles: List[Dict[str, Any]], granularity: str) -> List[Dict[str, Any]]:
    df = candles_to_df(candles)
    if df.empty:
        return []
    return _df_to_candles(resample_ohlcv(df, granularity))


def _df_to_candles(df: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for idx, row in df.iterrows():
        out.append({
            "time": idx.isoformat(), "complete": True,
            "mid": {"o": float(row["open"]), "h": float(row["high"]),
                    "l": float(row["low"]), "c": float(row["close"])},
            "volume": float(row.get("volume", 0) or 0),
        })
    return out


def _at(seq, i):
    return seq[i] if i < len(seq) else None
