"""ペーパートレード・ブローカー（リアル価格＋仮想約定）。

本物の値動き（market_data 経由の無料データ）でロボットを動かしつつ、
注文は仮想でシミュレーションする。口座・入金・本人確認は一切不要。

- OANDA と同一インターフェース（engine から差し替え可能）。
- 建玉は sqlite に永続化（run_multi が毎ティック新インスタンスを作っても
  状態が保たれる）。
- settle() で各建玉のストップ到達を判定し、到達していれば決済する。
  engine は run_once の冒頭で settle() を呼ぶ。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import Settings

PAPER_DB_PATH = os.environ.get("PAPER_DB_PATH", "instance/paper.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_positions (
    deal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    instrument TEXT NOT NULL,
    units INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    stop REAL,
    entry_time TEXT NOT NULL
);
"""


class PaperStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or PAPER_DB_PATH
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, account: str, instrument: str, units: int, entry: float,
            stop: Optional[float], entry_time: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO paper_positions (account, instrument, units, entry_price, stop, entry_time)"
            " VALUES (?,?,?,?,?,?)",
            (account, instrument, int(units), float(entry),
             None if stop is None else float(stop), entry_time),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list(self, account: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM paper_positions WHERE account=?", (account,)).fetchall()
        return [dict(r) for r in rows]

    def get(self, deal_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM paper_positions WHERE deal_id=?", (deal_id,)).fetchone()
        return dict(row) if row else None

    def update_stop(self, deal_id: str, stop: float) -> None:
        self._conn.execute("UPDATE paper_positions SET stop=? WHERE deal_id=?",
                           (float(stop), deal_id))
        self._conn.commit()

    def remove(self, deal_id: str) -> None:
        self._conn.execute("DELETE FROM paper_positions WHERE deal_id=?", (deal_id,))
        self._conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperBroker:
    def __init__(self, settings: Settings, store=None, market_data=None,
                 paper_store: Optional[PaperStore] = None) -> None:
        self.settings = settings
        self.account = getattr(settings, "paper_account", "default") or "default"
        self.balance = float(getattr(settings, "paper_balance", 10000.0))
        self.trade_store = store  # ダッシュボード用 TradeStore（任意）
        self._paper = paper_store or PaperStore()
        if market_data is None:
            from .market_data import YahooMarketData
            market_data = YahooMarketData()
        self.market = market_data

    # -- データ（リアル価格） -----------------------------------------------
    def get_candles(self, instrument: str, granularity: str, count: int = 500,
                    price: str = "M") -> List[Dict[str, Any]]:
        return self.market.get_candles(instrument, granularity, count=count)

    def get_pricing(self, instruments: List[str]) -> Dict[str, Any]:
        return self.market.get_pricing(instruments)

    def get_account_summary(self) -> Dict[str, Any]:
        return {"balance": self.balance, "currency": "JPY"}

    def get_open_trades(self) -> List[Dict[str, Any]]:
        out = []
        for p in self._paper.list(self.account):
            units = int(p["units"])
            out.append({
                "id": str(p["deal_id"]),
                "instrument": p["instrument"],
                "currentUnits": units,
                "initialUnits": units,
                "price": p["entry_price"],
                "stopLossOrder": {"price": p["stop"]} if p["stop"] is not None else None,
            })
        return out

    def get_trade(self, trade_id: str) -> Dict[str, Any]:
        return {}

    # -- 発注・決済（仮想） --------------------------------------------------
    def create_market_order(self, instrument: str, units: int,
                            stop_loss_price: Optional[float] = None,
                            client_id: Optional[str] = None,
                            price_precision: int = 5) -> Dict[str, Any]:
        fill = self._latest_price(instrument)
        deal_id = self._paper.add(self.account, instrument, units, fill,
                                  stop_loss_price, _now_iso())
        return {"orderFillTransaction": {"tradeOpened": {"tradeID": str(deal_id)},
                                         "price": fill}}

    def set_trade_stop_loss(self, trade_id: str, stop_loss_price: float,
                            price_precision: int = 5) -> Dict[str, Any]:
        self._paper.update_stop(trade_id, stop_loss_price)
        return {}

    def close_trade(self, trade_id: str, units: str = "ALL") -> Dict[str, Any]:
        pos = self._paper.get(trade_id)
        if pos is None:
            return {}
        self._close(pos, self._latest_price(pos["instrument"]), "manual")
        return {}

    # -- 決済判定（ストップ到達） -------------------------------------------
    def settle(self) -> int:
        """各建玉の現在価格をチェックし、ストップ到達なら決済する。件数を返す。"""
        closed = 0
        for pos in self._paper.list(self.account):
            stop = pos["stop"]
            if stop is None:
                continue
            price = self._latest_price(pos["instrument"])
            if price <= 0:
                continue
            units = int(pos["units"])
            hit = (price <= stop) if units > 0 else (price >= stop)
            if hit:
                self._close(pos, stop, "stop")  # ストップ価格で約定
                closed += 1
        return closed

    def _close(self, pos: Dict[str, Any], exit_price: float, reason: str) -> None:
        units = int(pos["units"])
        entry = float(pos["entry_price"])
        pnl_points = (exit_price - entry) if units > 0 else (entry - exit_price)
        pnl = pnl_points * abs(units)
        self._paper.remove(pos["deal_id"])
        if self.trade_store is not None:
            try:
                self.trade_store.record_close(
                    oanda_trade_id=str(pos["deal_id"]),
                    exit_time=_now_iso(), exit_price=exit_price,
                    pnl=pnl, exit_reason=reason)
            except Exception:  # noqa: BLE001
                pass

    def _latest_price(self, instrument: str) -> float:
        prices = self.get_pricing([instrument]).get(instrument, {})
        bid = prices.get("bids", [{}])[0].get("price") or 0
        ask = prices.get("asks", [{}])[0].get("price") or 0
        if bid and ask:
            return (float(bid) + float(ask)) / 2
        return float(bid or ask or 0)
