"""リスク管理: ポジションサイズ計算と損切り価格の決定。

ロットは「1トレードで失う額を口座の risk_per_trade%（例 0.5%）に固定」する
原則で決める。OANDA は units（基軸通貨の数量）建てなので、

    損失額(口座通貨) = units × ストップ幅(価格) × (quote→口座通貨 換算レート)

を risk_amount と一致させるよう units を逆算する。

quote_to_account_rate:
  通貨ペア BASE_QUOTE の quote 通貨を口座通貨へ換算するレート。
  口座通貨 == quote 通貨 のときは 1.0。それ以外は engine 側で
  現在価格から推定して渡す（未指定時は 1.0 = 近似）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .strategy import SIGNAL_BUY


@dataclass
class SizedOrder:
    instrument: str
    side: str          # BUY / SELL
    units: int         # 符号付き（買い正・売り負）
    entry_price: float
    stop_loss: float
    risk_amount: float  # 口座通貨での想定損失額
    oanda_trade_id: Optional[str] = None  # 約定後に付与される建玉ID


def stop_for(side: str, entry_price: float, atr: float, settings: Settings) -> float:
    """ATR ベースの初期損切り価格。"""
    dist = settings.atr_stop_mult * atr
    return entry_price - dist if side == SIGNAL_BUY else entry_price + dist


def position_units(
    balance: float,
    risk_pct: float,
    stop_distance: float,
    quote_to_account_rate: float = 1.0,
) -> int:
    """許容リスクから建玉数(units, 正の整数)を計算する。"""
    if balance <= 0 or stop_distance <= 0 or quote_to_account_rate <= 0:
        return 0
    risk_amount = balance * (risk_pct / 100.0)
    units = risk_amount / (stop_distance * quote_to_account_rate)
    return int(units)  # 切り捨て（リスク超過を避ける）


def build_order(
    instrument: str,
    side: str,
    entry_price: float,
    atr: float,
    balance: float,
    settings: Settings,
    quote_to_account_rate: float = 1.0,
) -> SizedOrder:
    """シグナルと口座状況から、サイズと SL を確定した注文を組み立てる。"""
    stop = stop_for(side, entry_price, atr, settings)
    stop_distance = abs(entry_price - stop)
    magnitude = position_units(
        balance, settings.risk_per_trade, stop_distance, quote_to_account_rate
    )
    units = magnitude if side == SIGNAL_BUY else -magnitude
    risk_amount = magnitude * stop_distance * quote_to_account_rate
    return SizedOrder(
        instrument=instrument,
        side=side,
        units=units,
        entry_price=entry_price,
        stop_loss=stop,
        risk_amount=risk_amount,
    )
