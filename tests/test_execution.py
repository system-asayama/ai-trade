"""Phase 2（執行・リスク・安全装置・エンジン）のテスト。

OANDA に接続せず、FakeClient で API 呼び出しを記録/模擬する。
`python tests/test_execution.py` で実行可能（pytest 不要）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading import risk  # noqa: E402
from trading.config import Settings  # noqa: E402
from trading.engine import TradingEngine  # noqa: E402
from trading.executor import Executor, OpenTrade, parse_open_trades  # noqa: E402
from trading.safety import CircuitBreaker  # noqa: E402
from trading.strategy import SIGNAL_BUY, SIGNAL_SELL  # noqa: E402


def settings(**env) -> Settings:
    defaults = {"EMA_SLOW": "100", "INSTRUMENTS": "USD_JPY"}
    defaults.update({k: str(v) for k, v in env.items()})
    for k, v in defaults.items():
        os.environ[k] = v
    return Settings()


# --- テスト用データ ---------------------------------------------------------
def make_uptrend(n: int = 200, step: float = 0.3, wick: float = 0.05) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    close = 100.0 + np.arange(n) * step
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = np.full(n, 100.0)
    volume[-1] = 500.0  # 直近で出来高増
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


class FakeFeed:
    """事前構築した足を返すフィード。"""

    def __init__(self, settings: Settings, frames):
        self.settings = settings
        self._frames = frames

    def fetch_multi_timeframe(self, instrument, count=500):
        return dict(self._frames)


class FakeClient:
    def __init__(self, balance=10000.0, currency="JPY"):
        self.summary = {"balance": balance, "currency": currency}
        self.open = []
        self.trades = {}
        self.prices = {}
        self.orders = []
        self.stop_updates = []
        self.closes = []

    def get_account_summary(self):
        return self.summary

    def get_open_trades(self):
        return self.open

    def get_trade(self, trade_id):
        return self.trades.get(trade_id, {})

    def get_pricing(self, instruments):
        return {i: self.prices.get(i, {"bids": [{"price": "150.0"}],
                                       "asks": [{"price": "150.02"}]})
                for i in instruments}

    def create_market_order(self, instrument, units, stop_loss_price=None,
                            client_id=None, price_precision=5):
        self.orders.append({"instrument": instrument, "units": units,
                            "stop_loss_price": stop_loss_price, "client_id": client_id})
        return {"orderFillTransaction": {"id": "1"}}

    def set_trade_stop_loss(self, trade_id, stop_loss_price, price_precision=5):
        self.stop_updates.append({"trade_id": trade_id, "price": stop_loss_price})
        return {}

    def close_trade(self, trade_id, units="ALL"):
        self.closes.append(trade_id)
        return {}


# --- risk ------------------------------------------------------------------
def test_position_units_math():
    # 残高1万・リスク1%・ストップ幅0.5・換算1.0 → 100/0.5 = 200 units
    units = risk.position_units(10000, 1.0, 0.5, 1.0)
    assert units == 200


def test_build_order_sell_is_negative():
    s = settings(RISK_PER_TRADE=1.0, ATR_STOP_MULT=1.5)
    order = risk.build_order("EUR_USD", SIGNAL_SELL, 1.10, atr=0.0010,
                             balance=10000, settings=s)
    assert order.units < 0
    assert order.stop_loss > order.entry_price  # 売りの SL は上


def test_build_order_zero_atr_no_units():
    s = settings()
    order = risk.build_order("USD_JPY", SIGNAL_BUY, 150.0, atr=0.0,
                             balance=10000, settings=s)
    assert order.units == 0


# --- executor --------------------------------------------------------------
def test_parse_open_trades():
    raw = [{"id": "55", "instrument": "USD_JPY", "currentUnits": "100",
            "initialUnits": "100", "price": "150.0",
            "stopLossOrder": {"price": "149.0"}}]
    trades = parse_open_trades(raw)
    assert trades[0].trade_id == "55"
    assert trades[0].side == SIGNAL_BUY
    assert trades[0].current_stop == 149.0


def test_trailing_raises_long_stop():
    s = settings(ATR_TRAIL_MULT=2.0)
    client = FakeClient()
    client.prices["USD_JPY"] = {"bids": [{"price": "150.00"}], "asks": [{"price": "150.00"}]}
    ex = Executor(s, client)
    tr = OpenTrade(trade_id="9", instrument="USD_JPY", units=100,
                   entry_price=148.0, current_stop=147.0, initial_units=100)
    updated = ex.trail_stops([tr], {"USD_JPY": 0.20})
    # 新ストップ = 150.0 - 2*0.20 = 149.6 > 147.0 なので引き上げられる
    assert updated == 1
    assert client.stop_updates[0]["price"] > 147.0
    assert tr.current_stop > 147.0


def test_trailing_never_loosens_stop():
    s = settings(ATR_TRAIL_MULT=2.0)
    client = FakeClient()
    client.prices["USD_JPY"] = {"bids": [{"price": "150.00"}], "asks": [{"price": "150.00"}]}
    ex = Executor(s, client)
    tr = OpenTrade(trade_id="9", instrument="USD_JPY", units=100,
                   entry_price=148.0, current_stop=149.8, initial_units=100)
    # 候補 149.6 < 既存 149.8 → 更新しない
    updated = ex.trail_stops([tr], {"USD_JPY": 0.20})
    assert updated == 0
    assert not client.stop_updates


# --- safety ----------------------------------------------------------------
def test_breaker_consecutive_losses_trips():
    s = settings()
    cb = CircuitBreaker(s, max_consecutive_losses=2)
    cb.register_close(-10.0, "2026-06-30")
    ok, _ = cb.can_open(0, "2026-06-30")
    assert ok
    cb.register_close(-10.0, "2026-06-30")
    ok, reason = cb.can_open(0, "2026-06-30")
    assert not ok and "tripped" in reason


def test_breaker_daily_loss_trips():
    s = settings()
    cb = CircuitBreaker(s, max_daily_loss=50.0)
    cb.register_close(-60.0, "2026-06-30")
    ok, reason = cb.can_open(0, "2026-06-30")
    assert not ok


def test_breaker_resets_next_day():
    s = settings()
    cb = CircuitBreaker(s, max_daily_loss=50.0)
    cb.register_close(-60.0, "2026-06-30")
    assert not cb.can_open(0, "2026-06-30")[0]
    # 翌日に自動復帰
    assert cb.can_open(0, "2026-07-01")[0]


def test_kill_switch_blocks_and_persists(tmp_path="/tmp/claude-0/cb.json"):
    s = settings()
    cb = CircuitBreaker.load(s, path=tmp_path)
    cb.kill()
    assert not cb.can_open(0, "2026-06-30")[0]
    # 再ロードしても kill 状態が残る
    cb2 = CircuitBreaker.load(s, path=tmp_path)
    assert cb2.state.killed
    os.remove(tmp_path)


# --- engine ----------------------------------------------------------------
def test_engine_opens_position_on_signal():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    up = make_uptrend()
    frames = {"M15": up, "H1": up, "H4": up, "D": up}
    client = FakeClient()
    engine = TradingEngine(s, client, feed=FakeFeed(s, frames),
                           breaker=CircuitBreaker(s))
    res = engine.run_once("2026-06-30")
    assert res.entries, f"エントリーが発生しなかった: blocked={res.blocked}"
    order = client.orders[0]
    assert order["units"] > 0  # 買い
    # 買いの SL はエントリー価格(直近終値)より下
    assert order["stop_loss_price"] is not None
    assert order["stop_loss_price"] < up["close"].iloc[-1]
    assert order["client_id"]  # 重複防止IDが付与される


def test_engine_blocks_entry_when_breaker_tripped():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    up = make_uptrend()
    frames = {"M15": up, "H1": up, "H4": up, "D": up}
    client = FakeClient()
    cb = CircuitBreaker(s, max_consecutive_losses=1)
    cb.register_close(-10.0, "2026-06-30")  # 即 trip
    engine = TradingEngine(s, client, feed=FakeFeed(s, frames), breaker=cb)
    res = engine.run_once("2026-06-30")
    assert not client.orders
    assert res.blocked.get("USD_JPY", "").startswith("tripped")


def test_engine_kill_switch_closes_all():
    s = settings(INSTRUMENTS="USD_JPY")
    client = FakeClient()
    client.open = [{"id": "7", "instrument": "USD_JPY", "currentUnits": "100",
                    "initialUnits": "100", "price": "150.0"}]
    cb = CircuitBreaker(s)
    cb.state.killed = True
    engine = TradingEngine(s, client, feed=FakeFeed(s, {}), breaker=cb)
    res = engine.run_once("2026-06-30")
    assert res.killed
    assert client.closes == ["7"]


def test_engine_registers_closed_trade_pnl():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    up = make_uptrend()
    frames = {"M15": up, "H1": up, "H4": up, "D": up}
    client = FakeClient()
    cb = CircuitBreaker(s, max_consecutive_losses=1)
    engine = TradingEngine(s, client, feed=FakeFeed(s, frames), breaker=cb)
    # 1回目: 建玉 "7" が存在
    client.open = [{"id": "7", "instrument": "USD_JPY", "currentUnits": "100",
                    "initialUnits": "100", "price": "150.0"}]
    engine.run_once("2026-06-30")
    # 2回目: "7" が消え、負けで決済済み → ブレーカーが trip
    client.open = []
    client.trades["7"] = {"id": "7", "realizedPL": "-20.0"}
    res = engine.run_once("2026-06-30")
    assert res.closes_registered == 1
    assert cb.state.tripped


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
