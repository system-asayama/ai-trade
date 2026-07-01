"""Capital.com クライアントとブローカー切替のテスト（ネットワーク非依存）。

Fake セッションで Capital.com REST を模擬し、OANDA と同一インターフェースで
動くこと（足変換・認証・発注・建玉・口座）を検証する。
`python tests/test_capital.py` で実行可能。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.broker import BROKER_CAPITAL, make_broker_client  # noqa: E402
from trading.capital_client import CapitalClient, to_epic  # noqa: E402
from trading.config import Settings  # noqa: E402
from trading.data_feed import candles_to_df  # noqa: E402
from trading.executor import parse_open_trades  # noqa: E402


def settings(**env):
    for k, v in {"INSTRUMENTS": "USD_JPY,EUR_USD"}.items():
        os.environ.setdefault(k, v)
    s = Settings()
    s.broker = "capital"
    s.capital_api_key = "APIKEY"
    s.capital_identifier = "me@example.com"
    s.capital_password = "pw"
    s.capital_env = "demo"
    for k, v in env.items():
        setattr(s, k, v)
    return s


# --- Fake セッション -------------------------------------------------------
class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.headers = headers or {}
        self.text = json.dumps(self._payload) if payload is not None else ""

    def json(self):
        return self._payload


class _Session:
    def __init__(self, routes):
        self.routes = routes  # list of (method, substr, _Resp)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers or {}, "params": params})
        for m, sub, resp in self.routes:
            if m == method and sub in url:
                return resp
        return _Resp({}, 404)


def _routes():
    return [
        ("POST", "/session", _Resp({}, 200, {"CST": "cst1", "X-SECURITY-TOKEN": "xst1"})),
        ("GET", "/prices/USDJPY", _Resp({"prices": [{
            "snapshotTimeUTC": "2024-01-01T00:00:00",
            "openPrice": {"bid": 150.0, "ask": 150.02},
            "highPrice": {"bid": 150.5, "ask": 150.52},
            "lowPrice": {"bid": 149.5, "ask": 149.52},
            "closePrice": {"bid": 150.2, "ask": 150.22},
            "lastTradedVolume": 100,
        }]})),
        ("POST", "/positions", _Resp({"dealReference": "ref1"})),
        ("GET", "/confirms/ref1", _Resp({"dealId": "D123"})),
        ("GET", "/positions", _Resp({"positions": [{
            "position": {"dealId": "D1", "direction": "BUY", "size": 1000,
                         "level": 150.0, "stopLevel": 149.0},
            "market": {"epic": "USDJPY"},
        }]})),
        ("GET", "/accounts", _Resp({"accounts": [
            {"preferred": True, "balance": {"balance": 10000.0}, "currency": "USD"}]})),
        ("PUT", "/positions/D1", _Resp({})),
        ("DELETE", "/positions/D1", _Resp({})),
    ]


def test_to_epic():
    assert to_epic("USD_JPY") == "USDJPY"
    assert to_epic("EUR_USD") == "EURUSD"


def test_auth_session_created_with_api_key():
    c = CapitalClient(settings(), session=_Session(_routes()))
    c.get_account_summary()
    first = c._session.calls[0]
    assert first["method"] == "POST" and "/session" in first["url"]
    assert first["headers"]["X-CAP-API-KEY"] == "APIKEY"
    # 後続リクエストはセッショントークン付き
    later = c._session.calls[1]
    assert later["headers"]["CST"] == "cst1"
    assert later["headers"]["X-SECURITY-TOKEN"] == "xst1"


def test_candles_convert_to_oanda_shape():
    c = CapitalClient(settings(), session=_Session(_routes()))
    candles = c.get_candles("USD_JPY", "M15", count=10)
    assert candles[0]["complete"] is True
    assert abs(candles[0]["mid"]["o"] - 150.01) < 1e-9  # (150.00+150.02)/2
    # data_feed で DataFrame 化できる
    df = candles_to_df(candles)
    assert len(df) == 1 and abs(df["close"].iloc[0] - 150.21) < 1e-9


def test_create_market_order_body_and_dealid():
    c = CapitalClient(settings(), session=_Session(_routes()))
    resp = c.create_market_order("USD_JPY", 1000, stop_loss_price=149.0, price_precision=3)
    # /positions POST のボディを検証
    post = next(call for call in c._session.calls if call["method"] == "POST"
                and "/positions" in call["url"])
    assert post["json"]["epic"] == "USDJPY"
    assert post["json"]["direction"] == "BUY"
    assert post["json"]["size"] == 1000
    assert post["json"]["stopLevel"] == 149.0
    # dealReference → dealId 解決
    assert resp["orderFillTransaction"]["tradeOpened"]["tradeID"] == "D123"


def test_sell_order_direction():
    c = CapitalClient(settings(), session=_Session(_routes()))
    c.create_market_order("USD_JPY", -500)
    post = next(call for call in c._session.calls if call["method"] == "POST"
                and "/positions" in call["url"])
    assert post["json"]["direction"] == "SELL" and post["json"]["size"] == 500


def test_open_trades_shape_and_parse():
    c = CapitalClient(settings(), session=_Session(_routes()))
    raw = c.get_open_trades()
    assert raw[0]["instrument"] == "USD_JPY"  # epic→instrument 逆引き
    assert raw[0]["currentUnits"] == 1000
    # executor.parse_open_trades でそのまま扱える
    trades = parse_open_trades(raw)
    assert trades[0].trade_id == "D1"
    assert trades[0].current_stop == 149.0


def test_account_summary():
    c = CapitalClient(settings(), session=_Session(_routes()))
    s = c.get_account_summary()
    assert s["balance"] == 10000.0 and s["currency"] == "USD"


# --- ブローカー切替 --------------------------------------------------------
def test_factory_selects_capital():
    c = make_broker_client(settings(), session=_Session(_routes()))
    assert isinstance(c, CapitalClient)


def test_factory_defaults_to_oanda():
    from trading.oanda_client import OandaClient
    s = Settings()
    s.broker = "oanda"
    c = make_broker_client(s, session=object())
    assert isinstance(c, OandaClient)


def test_settings_from_user_maps_capital():
    from trading.tenant import settings_from_user

    class _US:
        broker = "capital"
        oanda_account_id = ""
        oanda_env = "practice"
        instruments = "USD_JPY"
        risk_per_trade = 0.5
        max_open_positions = 2
        econ_blackout_before_min = 30
        econ_blackout_after_min = 15
        capital_identifier = "me@example.com"
        capital_env = "demo"

        def get_oanda_token(self):
            return ""

        def get_capital_api_key(self):
            return "K"

        def get_capital_password(self):
            return "P"

    s = settings_from_user(_US())
    assert s.broker == "capital"
    assert s.capital_api_key == "K" and s.capital_password == "P"
    assert s.capital_identifier == "me@example.com"


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
