"""経済指標カレンダー（危険度フィルタ）のテスト。

Static / HTTP プロバイダと engine ブラックアウトを検証。ネットワーク非依存。
`python tests/test_calendar.py` で実行可能。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from trading import calendar as cal  # noqa: E402
from trading.calendar import (  # noqa: E402
    EconomicCalendar,
    EconomicEvent,
    HttpCalendarProvider,
    StaticCalendarProvider,
    normalize_importance,
    parse_time,
)
from trading.config import Settings  # noqa: E402
from trading.engine import TradingEngine  # noqa: E402
from trading.safety import CircuitBreaker  # noqa: E402


def settings(**env):
    defaults = {"EMA_SLOW": "100", "INSTRUMENTS": "USD_JPY"}
    defaults.update({k: str(v) for k, v in env.items()})
    for k, v in defaults.items():
        os.environ[k] = v
    return Settings()


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# --- 正規化 ----------------------------------------------------------------
def test_normalize_importance():
    assert normalize_importance("High") == cal.IMPORTANCE_HIGH
    assert normalize_importance(3) == cal.IMPORTANCE_HIGH
    assert normalize_importance("3") == cal.IMPORTANCE_HIGH
    assert normalize_importance("★★★") == cal.IMPORTANCE_HIGH
    assert normalize_importance("medium") == cal.IMPORTANCE_MEDIUM
    assert normalize_importance("low") == cal.IMPORTANCE_LOW
    assert normalize_importance(None) == cal.IMPORTANCE_LOW


def test_parse_time_iso_and_epoch():
    assert parse_time("2026-06-30T12:00:00Z") == _utc(2026, 6, 30, 12, 0)
    assert parse_time("2026-06-30T12:00:00+00:00") == _utc(2026, 6, 30, 12, 0)
    # epoch 秒
    epoch = _utc(2026, 6, 30, 12, 0).timestamp()
    assert parse_time(epoch) == _utc(2026, 6, 30, 12, 0)
    assert parse_time("") is None


# --- ブラックアウト判定 -----------------------------------------------------
def _calendar(events, **env):
    s = settings(**env)
    c = EconomicCalendar(StaticCalendarProvider(events), s)
    c.refresh()
    return c


def test_blackout_before_and_after_window():
    ev = EconomicEvent(_utc(2026, 6, 30, 12, 0), "USD", "high", "NFP")
    c = _calendar([ev], ECON_BLACKOUT_BEFORE_MIN=30, ECON_BLACKOUT_AFTER_MIN=15)
    # 25分前 → ブラックアウト
    assert c.is_blackout("USD_JPY", _utc(2026, 6, 30, 11, 35))[0]
    # 10分後 → ブラックアウト
    assert c.is_blackout("USD_JPY", _utc(2026, 6, 30, 12, 10))[0]
    # 40分前 → 窓外
    assert not c.is_blackout("USD_JPY", _utc(2026, 6, 30, 11, 20))[0]
    # 20分後 → 窓外
    assert not c.is_blackout("USD_JPY", _utc(2026, 6, 30, 12, 20))[0]


def test_blackout_currency_must_match():
    ev = EconomicEvent(_utc(2026, 6, 30, 12, 0), "USD", "high", "NFP")
    c = _calendar([ev])
    # EUR_USD は USD を含むのでヒット
    assert c.is_blackout("EUR_USD", _utc(2026, 6, 30, 12, 0))[0]
    # GBP_AUD は無関係
    assert not c.is_blackout("GBP_AUD", _utc(2026, 6, 30, 12, 0))[0]


def test_blackout_importance_threshold():
    ev = EconomicEvent(_utc(2026, 6, 30, 12, 0), "JPY", "medium", "minor")
    c = _calendar([ev], ECON_IMPORTANCE_MIN="high")
    # medium は high 閾値未満 → ブラックアウトしない
    assert not c.is_blackout("USD_JPY", _utc(2026, 6, 30, 12, 0))[0]


# --- HTTP プロバイダ --------------------------------------------------------
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.last = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.last = {"url": url, "params": params, "headers": headers}
        return _FakeResp(self._payload)


def test_http_provider_parses_events():
    payload = [
        {"date": "2026-06-30T12:00:00Z", "country": "usd", "impact": "High", "title": "NFP"},
        {"date": "2026-06-30T13:00:00Z", "country": "eur", "impact": "Low", "title": "x"},
        {"country": "jpy", "impact": "High"},  # 時刻欠落 → スキップ
    ]
    provider = HttpCalendarProvider("http://example/cal", session=_FakeSession(payload))
    events = provider.fetch_events()
    assert len(events) == 2
    assert events[0].currency == "USD" and events[0].importance == "high"


# --- engine 統合 -----------------------------------------------------------
def _uptrend(n=200, step=0.3, wick=0.05):
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    close = 100.0 + np.arange(n) * step
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    vol = np.full(n, 100.0)
    vol[-1] = 500.0
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


class _Feed:
    def __init__(self, frames):
        self._frames = frames

    def fetch_multi_timeframe(self, instrument, count=500):
        return dict(self._frames)


class _Client:
    def __init__(self):
        self.summary = {"balance": 10000.0, "currency": "JPY"}
        self.orders = []

    def get_account_summary(self):
        return self.summary

    def get_open_trades(self):
        return []

    def get_trade(self, tid):
        return {}

    def create_market_order(self, instrument, units, stop_loss_price=None,
                            client_id=None, price_precision=5):
        self.orders.append({"units": units})
        return {"orderFillTransaction": {"tradeOpened": {"tradeID": "1"}}}


def test_engine_blocks_entry_during_econ_blackout():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    frames = {"M15": _uptrend(), "H1": _uptrend(), "H4": _uptrend(), "D": _uptrend()}
    bar_time = frames["M15"].index[-1].to_pydatetime()
    # 直近バー時刻に重なる USD 高重要度イベント
    ev = EconomicEvent(bar_time, "USD", "high", "FOMC")
    calendar = EconomicCalendar(StaticCalendarProvider([ev]), s)
    calendar.refresh()
    client = _Client()
    engine = TradingEngine(s, client, feed=_Feed(frames),
                           breaker=CircuitBreaker(s), calendar=calendar)
    res = engine.run_once("2026-06-30")
    assert not client.orders
    assert res.blocked.get("USD_JPY") == "econ_blackout:USD"


def test_engine_allows_when_no_nearby_event():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    frames = {"M15": _uptrend(), "H1": _uptrend(), "H4": _uptrend(), "D": _uptrend()}
    bar_time = frames["M15"].index[-1].to_pydatetime()
    ev = EconomicEvent(bar_time + timedelta(hours=5), "USD", "high", "later")
    calendar = EconomicCalendar(StaticCalendarProvider([ev]), s)
    calendar.refresh()
    client = _Client()
    engine = TradingEngine(s, client, feed=_Feed(frames),
                           breaker=CircuitBreaker(s), calendar=calendar)
    engine.run_once("2026-06-30")
    assert client.orders  # 近接イベントなし → 通常どおり発注


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
