"""ペーパートレード（リアル価格＋仮想約定）のテスト。ネットワーク非依存。

`python tests/test_paper.py` で実行可能。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading import market_data as md  # noqa: E402
from trading.broker import BROKER_PAPER, make_broker_client  # noqa: E402
from trading.config import Settings  # noqa: E402
from trading.data_feed import candles_to_df  # noqa: E402
from trading.engine import TradingEngine  # noqa: E402
from trading.market_data import YahooMarketData, to_symbol  # noqa: E402
from trading.paper_broker import PaperBroker, PaperStore  # noqa: E402
from trading.safety import CircuitBreaker  # noqa: E402


def settings(**env):
    for k, v in {"EMA_SLOW": "100", "INSTRUMENTS": "USD_JPY"}.items():
        os.environ[k] = v
    s = Settings()
    s.broker = "paper"
    for k, v in env.items():
        setattr(s, k, v)
    return s


# --- market_data -----------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self._p = payload
        self.last = None

    def get(self, url, params=None, headers=None, timeout=None):
        self.last = {"url": url, "params": params, "headers": headers}
        return _Resp(self._p)


def _yahoo_payload():
    ts = [1704067200, 1704068100, 1704069000]  # 15分間隔
    return {"chart": {"result": [{
        "timestamp": ts,
        "indicators": {"quote": [{
            "open": [150.0, 150.1, 150.2],
            "high": [150.2, 150.3, 150.4],
            "low": [149.9, 150.0, 150.1],
            "close": [150.1, 150.2, 150.3],
            "volume": [100, 110, 120],
        }]},
    }]}}


def test_to_symbol():
    assert to_symbol("USD_JPY") == "USDJPY=X"


def test_yahoo_parse_to_oanda_shape():
    y = YahooMarketData(session=_Session(_yahoo_payload()))
    candles = y.get_candles("USD_JPY", "M15")
    assert len(candles) == 3
    assert candles[0]["complete"] is True
    assert candles[0]["mid"]["c"] == 150.1
    df = candles_to_df(candles)
    assert len(df) == 3 and df["close"].iloc[-1] == 150.3


def test_yahoo_skips_null_bars():
    p = _yahoo_payload()
    p["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    y = YahooMarketData(session=_Session(p))
    assert len(y.get_candles("USD_JPY", "M15")) == 2


# --- PaperBroker -----------------------------------------------------------
class FakeMarket:
    def __init__(self, price=100.0, candles=None):
        self.price = price
        self._candles = candles

    def get_candles(self, instrument, granularity, count=500, price="M"):
        return (self._candles or [])[-count:] if count else (self._candles or [])

    def get_pricing(self, instruments):
        return {i: {"bids": [{"price": self.price}], "asks": [{"price": self.price}]}
                for i in instruments}


class FakeStore:
    def __init__(self):
        self.closes = []

    def record_close(self, **kw):
        self.closes.append(kw)


def test_paper_open_and_list():
    b = PaperBroker(settings(), store=FakeStore(), market_data=FakeMarket(150.0),
                    paper_store=PaperStore(":memory:"))
    resp = b.create_market_order("USD_JPY", 1000, stop_loss_price=149.0)
    tid = resp["orderFillTransaction"]["tradeOpened"]["tradeID"]
    trades = b.get_open_trades()
    assert len(trades) == 1
    assert trades[0]["id"] == tid
    assert trades[0]["currentUnits"] == 1000
    assert trades[0]["stopLossOrder"]["price"] == 149.0


def test_paper_settle_closes_on_stop():
    store = FakeStore()
    # 現在価格 148 は long のストップ 149 を下回る → 決済されるはず
    b = PaperBroker(settings(), store=store, market_data=FakeMarket(148.0),
                    paper_store=PaperStore(":memory:"))
    b.create_market_order("USD_JPY", 1000, stop_loss_price=149.0)
    closed = b.settle()
    assert closed == 1
    assert b.get_open_trades() == []
    # ダッシュボード用ストアへ決済が記録される
    assert store.closes and store.closes[0]["exit_price"] == 149.0
    assert store.closes[0]["exit_reason"] == "stop"


def test_paper_settle_keeps_position_when_no_hit():
    b = PaperBroker(settings(), store=FakeStore(), market_data=FakeMarket(151.0),
                    paper_store=PaperStore(":memory:"))
    b.create_market_order("USD_JPY", 1000, stop_loss_price=149.0)
    assert b.settle() == 0
    assert len(b.get_open_trades()) == 1


def test_paper_short_settle():
    store = FakeStore()
    # short（units<0）、ストップ 151、価格 152 で到達
    b = PaperBroker(settings(), store=store, market_data=FakeMarket(152.0),
                    paper_store=PaperStore(":memory:"))
    b.create_market_order("USD_JPY", -1000, stop_loss_price=151.0)
    assert b.settle() == 1


def test_paper_manual_close():
    store = FakeStore()
    b = PaperBroker(settings(), store=store, market_data=FakeMarket(150.0),
                    paper_store=PaperStore(":memory:"))
    r = b.create_market_order("USD_JPY", 1000, stop_loss_price=149.0)
    tid = r["orderFillTransaction"]["tradeOpened"]["tradeID"]
    b.close_trade(tid)
    assert b.get_open_trades() == []
    assert store.closes


def test_factory_paper():
    c = make_broker_client(settings(), store=FakeStore())
    assert isinstance(c, PaperBroker)


# --- engine 統合（ペーパーで実際にエントリー） -----------------------------
def _uptrend_candles(n=200, step=0.3, wick=0.05):
    out = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        c = 100.0 + i * step
        o = c - step if i > 0 else c
        out.append({
            "time": (start + timedelta(minutes=15 * i)).isoformat(),
            "complete": True,
            "mid": {"o": o, "h": max(o, c) + wick, "l": min(o, c) - wick, "c": c},
            "volume": 500 if i == n - 1 else 100,
        })
    return out


def test_engine_paper_entry_records_position():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS="2")
    market = FakeMarket(price=100.0 + 199 * 0.3, candles=_uptrend_candles())
    ps = PaperStore(":memory:")
    broker = PaperBroker(s, store=FakeStore(), market_data=market, paper_store=ps)
    engine = TradingEngine(s, broker, breaker=CircuitBreaker(s))
    res = engine.run_once("2026-06-30")
    # 上昇トレンドのブレイクで買いエントリーし、ペーパー建玉が1件できる
    assert res.entries, f"エントリーが発生しなかった: blocked={res.blocked}"
    assert len(broker.get_open_trades()) == 1


def _run_all():
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
