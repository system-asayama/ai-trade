"""ブローカー選択のファクトリ。

settings.broker に応じて ペーパー / OANDA のクライアントを返す。
どちらも同一インターフェース（get_candles / get_pricing / get_open_trades /
create_market_order / set_trade_stop_loss / close_trade / get_account_summary /
get_trade）を実装しているため、エンジン側は差し替えを意識しない。
"""
from __future__ import annotations

from typing import Any

from .config import Settings

BROKER_OANDA = "oanda"
BROKER_PAPER = "paper"
BROKERS = (BROKER_OANDA, BROKER_PAPER)


def make_broker_client(settings: Settings, session: Any = None, store: Any = None):
    broker = getattr(settings, "broker", BROKER_OANDA)
    if broker == BROKER_PAPER:
        from .paper_broker import PaperBroker
        return PaperBroker(settings, store=store)
    from .oanda_client import OandaClient
    return OandaClient(settings, session=session)
