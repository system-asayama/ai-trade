"""取引の永続化ストア（Phase 3）。

標準ライブラリ sqlite3 のみで実装し、追加依存なしで動作・テストできる。
エンジン（書き込み）とダッシュボード（読み取り）の双方から利用される。

注: 設計上は Postgres も想定。ストアのインターフェースは小さいので、
将来 SQLAlchemy 実装へ差し替え可能（メソッド契約を維持する）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import metrics

DEFAULT_DB_PATH = os.environ.get("TRADES_DB_PATH", "instance/trading.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    units INTEGER NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL,
    exit_time TEXT,
    exit_price REAL,
    pnl REAL,
    r_multiple REAL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_reason TEXT,
    session TEXT,
    environment TEXT,
    oanda_trade_id TEXT,
    client_id TEXT,
    entry_features TEXT,
    created_at TEXT NOT NULL
);
"""


def _iso(value: Any) -> Optional[str]:
    """datetime / pandas.Timestamp / 文字列 を ISO 文字列へ正規化する。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    # pandas.Timestamp も isoformat を持つ
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class TradeStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        # 同一接続を保持（:memory: を保つため）。check_same_thread は緩める。
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- 書き込み ------------------------------------------------------------
    def record_open(
        self,
        instrument: str,
        side: str,
        units: int,
        entry_time: Any,
        entry_price: float,
        stop_loss: Optional[float],
        environment: str = "practice",
        oanda_trade_id: Optional[str] = None,
        client_id: Optional[str] = None,
        entry_features: Optional[Dict[str, Any]] = None,
        created_at: Optional[Any] = None,
    ) -> int:
        entry_iso = _iso(entry_time)
        session = metrics.classify_session(entry_iso)
        cur = self._conn.execute(
            """INSERT INTO trades
               (instrument, side, units, entry_time, entry_price, stop_loss,
                status, session, environment, oanda_trade_id, client_id,
                entry_features, created_at)
               VALUES (?,?,?,?,?,?, 'open', ?,?,?,?,?,?)""",
            (
                instrument, side, int(units), entry_iso, float(entry_price),
                None if stop_loss is None else float(stop_loss),
                session, environment, oanda_trade_id, client_id,
                json.dumps(entry_features or {}, ensure_ascii=False, default=str),
                _iso(created_at) or entry_iso,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_close(
        self,
        oanda_trade_id: str,
        exit_time: Any,
        exit_price: Optional[float],
        pnl: Optional[float],
        exit_reason: str = "",
    ) -> None:
        """オープン行を決済済みに更新する。該当が無ければ最小限の決済行を挿入。"""
        row = self._conn.execute(
            "SELECT * FROM trades WHERE oanda_trade_id=? AND status='open' "
            "ORDER BY id DESC LIMIT 1",
            (oanda_trade_id,),
        ).fetchone()

        r_multiple = None
        if row is not None and exit_price is not None and row["stop_loss"] is not None:
            r_multiple = _r_multiple(
                row["side"], row["entry_price"], row["stop_loss"], exit_price
            )

        if row is None:
            self._conn.execute(
                """INSERT INTO trades
                   (instrument, side, units, entry_time, entry_price, stop_loss,
                    exit_time, exit_price, pnl, r_multiple, status, exit_reason,
                    environment, oanda_trade_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'closed', ?,?,?,?)""",
                ("UNKNOWN", "UNKNOWN", 0, _iso(exit_time), exit_price or 0.0, None,
                 _iso(exit_time), exit_price, pnl, r_multiple, exit_reason,
                 "practice", oanda_trade_id, _iso(exit_time)),
            )
        else:
            self._conn.execute(
                """UPDATE trades SET exit_time=?, exit_price=?, pnl=?, r_multiple=?,
                   status='closed', exit_reason=? WHERE id=?""",
                (_iso(exit_time), exit_price, pnl, r_multiple, exit_reason, row["id"]),
            )
        self._conn.commit()

    # -- 読み取り ------------------------------------------------------------
    def list_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def closed_trades(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_time ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def open_count(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) AS c FROM trades WHERE status='open'"
            ).fetchone()["c"]
        )

    def close(self) -> None:
        self._conn.close()


def _r_multiple(side: str, entry: float, stop: float, exit_price: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    pnl_points = (exit_price - entry) if side == "BUY" else (entry - exit_price)
    return pnl_points / risk
