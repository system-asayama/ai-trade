"""執行: OANDA への発注・損切り更新（トレーリング）・全決済（キルスイッチ）。

ライブ口座の保護を最優先する:
- 既定は practice。live はサーキットブレーカー/キルスイッチを通過した時のみ。
- client_id による重複発注防止（同一バーの二重エントリーを避ける）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import Settings
from .oanda_client import OandaClient
from .risk import SizedOrder, build_order, stop_for
from .strategy import SIGNAL_BUY, Signal

logger = logging.getLogger("trading.executor")


@dataclass
class OpenTrade:
    """OANDA の openTrade を正規化した内部表現。"""

    trade_id: str
    instrument: str
    units: int            # 符号付き
    entry_price: float
    current_stop: Optional[float]
    initial_units: int

    @property
    def side(self) -> str:
        return SIGNAL_BUY if self.units > 0 else "SELL"


def _extract_trade_id(resp: dict) -> Optional[str]:
    """create_market_order レスポンスから建玉IDを抽出する（無ければ None）。"""
    fill = (resp or {}).get("orderFillTransaction") or {}
    opened = fill.get("tradeOpened") or {}
    trade_id = opened.get("tradeID")
    return str(trade_id) if trade_id is not None else None


def parse_open_trades(raw: List[dict]) -> List[OpenTrade]:
    """OANDA openTrades レスポンスを OpenTrade のリストへ変換する。"""
    trades: List[OpenTrade] = []
    for t in raw:
        units = int(float(t.get("currentUnits", t.get("initialUnits", 0))))
        stop = None
        sl = t.get("stopLossOrder")
        if sl and sl.get("price") is not None:
            stop = float(sl["price"])
        trades.append(
            OpenTrade(
                trade_id=str(t["id"]),
                instrument=t["instrument"],
                units=units,
                entry_price=float(t.get("price", 0.0)),
                current_stop=stop,
                initial_units=int(float(t.get("initialUnits", units))),
            )
        )
    return trades


class Executor:
    def __init__(self, settings: Settings, client: OandaClient,
                 price_precision: Dict[str, int] | None = None) -> None:
        self.settings = settings
        self.client = client
        # JPY クロスは小数3桁、その他は5桁が一般的
        self._precision = price_precision or {}

    def precision_for(self, instrument: str) -> int:
        if instrument in self._precision:
            return self._precision[instrument]
        return 3 if instrument.endswith("_JPY") else 5

    # -- 発注 ----------------------------------------------------------------
    def open_position(
        self,
        signal: Signal,
        instrument: str,
        balance: float,
        client_id: Optional[str] = None,
        quote_to_account_rate: float = 1.0,
        size_factor: float = 1.0,
    ) -> Optional[SizedOrder]:
        """シグナルに基づき成行＋SLで新規建玉する。units が 0 なら発注しない。

        size_factor: ニュース等の補助フィルタによるロット縮小係数（0〜1）。
        """
        order = build_order(
            instrument=instrument,
            side=signal.side,
            entry_price=signal.price,
            atr=signal.atr,
            balance=balance,
            settings=self.settings,
            quote_to_account_rate=quote_to_account_rate,
        )
        if size_factor < 1.0:
            magnitude = int(abs(order.units) * max(0.0, size_factor))
            order.units = magnitude if order.units > 0 else -magnitude
        if order.units == 0:
            logger.warning("units=0 のため発注スキップ: %s", instrument)
            return None

        resp = self.client.create_market_order(
            instrument=instrument,
            units=order.units,
            stop_loss_price=order.stop_loss,
            client_id=client_id,
            price_precision=self.precision_for(instrument),
        )
        order.oanda_trade_id = _extract_trade_id(resp)
        logger.info("発注: %s %s units=%d SL=%.5f id=%s",
                    instrument, signal.side, order.units, order.stop_loss,
                    order.oanda_trade_id)
        return order

    # -- トレーリング --------------------------------------------------------
    def trail_stops(self, trades: List[OpenTrade], atr_by_instrument: Dict[str, float]) -> int:
        """各トレードの SL を ATR トレーリングで建値方向にのみ更新する。更新件数を返す。"""
        updated = 0
        for tr in trades:
            atr = atr_by_instrument.get(tr.instrument)
            if not atr or tr.entry_price <= 0:
                continue
            # 直近価格の代わりにエントリー基準ではなく current price が必要だが、
            # ここでは保守的に「現在のストップを ATR 分だけ建値方向へ寄せる」方針。
            if tr.units > 0:  # ロング
                candidate = self._latest_close(tr.instrument) - self.settings.atr_trail_mult * atr
                if tr.current_stop is None or candidate > tr.current_stop:
                    self._update_stop(tr, candidate)
                    updated += 1
            else:  # ショート
                candidate = self._latest_close(tr.instrument) + self.settings.atr_trail_mult * atr
                if tr.current_stop is None or candidate < tr.current_stop:
                    self._update_stop(tr, candidate)
                    updated += 1
        return updated

    def _latest_close(self, instrument: str) -> float:
        prices = self.client.get_pricing([instrument]).get(instrument, {})
        bids = prices.get("bids") or [{}]
        asks = prices.get("asks") or [{}]
        bid = float(bids[0].get("price", 0) or 0)
        ask = float(asks[0].get("price", 0) or 0)
        if bid and ask:
            return (bid + ask) / 2
        return bid or ask

    def _update_stop(self, tr: OpenTrade, new_stop: float) -> None:
        self.client.set_trade_stop_loss(
            tr.trade_id, new_stop, price_precision=self.precision_for(tr.instrument)
        )
        tr.current_stop = new_stop
        logger.info("SL更新: trade=%s -> %.5f", tr.trade_id, new_stop)

    # -- キルスイッチ --------------------------------------------------------
    def close_all(self, trades: List[OpenTrade]) -> int:
        """全建玉を成行決済する。決済件数を返す。"""
        closed = 0
        for tr in trades:
            try:
                self.client.close_trade(tr.trade_id)
                closed += 1
                logger.warning("キルスイッチ決済: trade=%s", tr.trade_id)
            except Exception as exc:  # noqa: BLE001 個別失敗は記録して継続
                logger.error("決済失敗 trade=%s: %s", tr.trade_id, exc)
        return closed
