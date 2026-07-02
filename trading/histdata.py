"""HistData.com の長期ヒストリカルデータ取り込み（長期バックテスト用）。

HistData の「Generic ASCII / 1-minute Bars」を読み込み、15分足に集約して
ローカルに保存する。これにより **今の15分足ロジックのまま数年ぶん**の
バックテストができる。

CSV 形式: `YYYYMMDD HHMMSS;Open;High;Low;Close;Volume`（セミコロン区切り、
Bid値、時刻は EST=UTC-5・夏時間なし）。

- パーサ/リサンプル/保存はネットワーク非依存でテストできる。
- ダウンロードはベストエフォート（失敗時は手動DLしたファイルを取り込める）。
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .data_feed import resample_ohlcv

HIST_DB_PATH = os.environ.get("HIST_DB_PATH", "instance/histdata.db")
_EST_TO_UTC = timedelta(hours=5)  # EST(UTC-5, 夏時間なし) → UTC
_UA = "Mozilla/5.0 (compatible; ai-trade-hist/1.0)"


def to_pair(instrument: str) -> str:
    """'USD_JPY' -> 'usdjpy'（HistData のペア表記）。"""
    return instrument.replace("_", "").lower()


# --- パーサ ----------------------------------------------------------------
def parse_m1_text(text: str) -> pd.DataFrame:
    """HistData Generic ASCII M1 テキストを OHLCV DataFrame(UTC) に変換する。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) < 5:
            continue
        try:
            naive = datetime.strptime(parts[0], "%Y%m%d %H%M%S")
            t = (naive + _EST_TO_UTC).replace(tzinfo=timezone.utc)
            o, h, l, c = (float(parts[1]), float(parts[2]),
                          float(parts[3]), float(parts[4]))
            v = float(parts[5]) if len(parts) > 5 and parts[5] else 0.0
        except (ValueError, IndexError):
            continue
        rows.append((t, o, h, l, c, v))
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    return df.set_index("time").sort_index()


def parse_zip_bytes(data: bytes) -> pd.DataFrame:
    """HistData の ZIP（中に CSV）を読み込んで M1 DataFrame にする。

    ダウンロード失敗時に ZIP でない内容（HTML等）が来ても落ちないよう、
    不正な ZIP は空として扱う。
    """
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                return empty
            text = zf.read(names[0]).decode("utf-8", errors="ignore")
    except zipfile.BadZipFile:
        return empty
    return parse_m1_text(text)


def to_m15(df_m1: pd.DataFrame) -> pd.DataFrame:
    """M1 を M15 に集約する。"""
    if df_m1.empty:
        return df_m1
    return resample_ohlcv(df_m1, "M15")


# --- ローカル保存（sqlite） -------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_candles (
    instrument TEXT NOT NULL,
    granularity TEXT NOT NULL,
    time TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL, volume REAL,
    PRIMARY KEY (instrument, granularity, time)
);
"""


class HistStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or HIST_DB_PATH
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def import_df(self, instrument: str, granularity: str, df: pd.DataFrame) -> int:
        rows = [(instrument, granularity, idx.isoformat(),
                 float(r["open"]), float(r["high"]), float(r["low"]),
                 float(r["close"]), float(r.get("volume", 0) or 0))
                for idx, r in df.iterrows()]
        self._conn.executemany(
            "INSERT OR REPLACE INTO hist_candles "
            "(instrument, granularity, time, o, h, l, c, volume) VALUES (?,?,?,?,?,?,?,?)",
            rows)
        self._conn.commit()
        return len(rows)

    def load_candles(self, instrument: str, granularity: str = "M15",
                     limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """OANDA 互換のローソク足リストで返す（新しい順に limit 件→時系列に整列）。"""
        q = ("SELECT time, o, h, l, c, volume FROM hist_candles "
             "WHERE instrument=? AND granularity=? ORDER BY time ASC")
        rows = self._conn.execute(q, (instrument, granularity)).fetchall()
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return [{"time": r["time"], "complete": True,
                 "mid": {"o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]},
                 "volume": r["volume"]} for r in rows]

    def coverage(self, instrument: str, granularity: str = "M15") -> Tuple[Optional[str], Optional[str], int]:
        row = self._conn.execute(
            "SELECT MIN(time) mn, MAX(time) mx, COUNT(*) cnt FROM hist_candles "
            "WHERE instrument=? AND granularity=?", (instrument, granularity)).fetchone()
        return (row["mn"], row["mx"], int(row["cnt"] or 0))

    def instruments(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT instrument FROM hist_candles").fetchall()
        return [r["instrument"] for r in rows]

    def year_count(self, instrument: str, year: int,
                   granularity: str = "M15") -> int:
        """指定の年に保存済みの本数（一括取り込みで既存年をスキップするため）。"""
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM hist_candles "
            "WHERE instrument=? AND granularity=? AND substr(time,1,4)=?",
            (instrument, granularity, str(year))).fetchone()
        return int(row["c"] or 0)


# --- 取り込み ---------------------------------------------------------------
def import_m1_bytes(store: HistStore, instrument: str, data: bytes,
                    is_zip: bool = True) -> int:
    """M1 の ZIP/CSV バイト列を取り込み、M15 に集約して保存。保存件数を返す。"""
    df_m1 = parse_zip_bytes(data) if is_zip else parse_m1_text(data.decode("utf-8", "ignore"))
    m15 = to_m15(df_m1)
    if m15.empty:
        return 0
    return store.import_df(instrument, "M15", m15)


def _download(referer: str, session: Any) -> bytes:
    """HistData のダウンロードページのトークンを読み、ZIP を取得する。"""
    html = session.get(referer, headers={"User-Agent": _UA}, timeout=30).text

    def field(name: str, default: str = "") -> str:
        m = re.search(rf'id="{name}"[^>]*value="([^"]*)"', html) \
            or re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
        return m.group(1) if m else default

    data = {
        "tk": field("tk"),
        "date": field("date"),
        "datemonth": field("datemonth"),
        "platform": field("platform", "ASCII"),
        "timeframe": field("timeframe", "M1"),
        "fxpair": field("fxpair"),
    }
    resp = session.post("https://www.histdata.com/get.php", data=data,
                        headers={"User-Agent": _UA, "Referer": referer}, timeout=60)
    resp.raise_for_status()
    return resp.content


def _base_referer(pair: str) -> str:
    return ("https://www.histdata.com/download-free-forex-historical-data/"
            f"?/ascii/1-minute-bar-quotes/{pair}")


def download_year(instrument: str, year: int, session: Optional[Any] = None) -> bytes:
    """指定年の M1 ZIP を取得する（過去の完結した年向け・ベストエフォート）。"""
    import requests

    session = session or requests.Session()
    pair = to_pair(instrument)
    return _download(f"{_base_referer(pair)}/{year}", session)


def download_month(instrument: str, year: int, month: int,
                   session: Optional[Any] = None) -> bytes:
    """指定年月の M1 ZIP を取得する（進行中の年＝月別提供のとき用）。"""
    import requests

    session = session or requests.Session()
    pair = to_pair(instrument)
    return _download(f"{_base_referer(pair)}/{year}/{month}", session)
