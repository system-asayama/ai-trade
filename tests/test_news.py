"""Phase 4（ニュース/中銀発言解析とフィルタ）のテスト。

Anthropic クライアントを Fake で差し替え、ネットワーク非依存で検証する。
`python tests/test_news.py` で実行可能。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from trading import news  # noqa: E402
from trading.config import Settings  # noqa: E402
from trading.engine import TradingEngine  # noqa: E402
from trading.news import (  # noqa: E402
    NewsAnalyzer,
    NewsSentiment,
    SentimentStore,
    sentiment_filter,
)
from trading.safety import CircuitBreaker  # noqa: E402


# --- Fake Anthropic クライアント ------------------------------------------
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, payload):
        self.content = [_Block(json.dumps(payload))]


class FakeMessages:
    def __init__(self, payload, capture):
        self._payload = payload
        self._capture = capture

    def create(self, **kwargs):
        self._capture.update(kwargs)
        return _Resp(self._payload)


class FakeAnthropic:
    def __init__(self, payload):
        self.capture = {}
        self.messages = FakeMessages(payload, self.capture)


def settings(**env):
    defaults = {"EMA_SLOW": "100", "INSTRUMENTS": "USD_JPY"}
    defaults.update({k: str(v) for k, v in env.items()})
    for k, v in defaults.items():
        os.environ[k] = v
    return Settings()


# --- analyzer --------------------------------------------------------------
def test_analyzer_parses_structured_output():
    payload = {"bias": "bullish", "risk_level": "low", "confidence": 0.8,
               "event_type": "central_bank", "rationale": "利上げ示唆"}
    client = FakeAnthropic(payload)
    analyzer = NewsAnalyzer(client=client)
    s = analyzer.analyze("USD_JPY", "FRBが追加利上げを示唆")
    assert s.bias == news.BIAS_BULLISH
    assert s.risk_level == news.RISK_LOW
    assert abs(s.confidence - 0.8) < 1e-9
    assert s.event_type == "central_bank"
    # 正しいモデルと構造化出力スキーマで呼ばれている
    assert client.capture["model"] == "claude-opus-4-8"
    assert client.capture["output_config"]["format"]["type"] == "json_schema"


def test_analyzer_clamps_confidence():
    payload = {"bias": "bearish", "risk_level": "high", "confidence": 1.7,
               "event_type": "other", "rationale": "x"}
    s = NewsAnalyzer(client=FakeAnthropic(payload)).analyze("EUR_USD", "test")
    assert s.confidence == 1.0


# --- sentiment_filter ------------------------------------------------------
def _sent(bias, risk, conf):
    return NewsSentiment("USD_JPY", bias, risk, conf, "other", "")


def test_filter_no_news_allows():
    d = sentiment_filter("BUY", None)
    assert d.allow and d.size_factor == 1.0 and d.reason == "no_news"


def test_filter_high_risk_blocks():
    d = sentiment_filter("BUY", _sent(news.BIAS_BULLISH, news.RISK_HIGH, 0.9))
    assert not d.allow and d.reason == "news_high_risk"


def test_filter_contradiction_blocks():
    # BUY なのに高確信の bearish → 見送り
    d = sentiment_filter("BUY", _sent(news.BIAS_BEARISH, news.RISK_LOW, 0.7))
    assert not d.allow and d.reason == "news_contradicts"


def test_filter_weak_contradiction_allows_reduced():
    # 低確信の逆風 → 通すがサイズ縮小
    d = sentiment_filter("BUY", _sent(news.BIAS_BEARISH, news.RISK_LOW, 0.3))
    assert d.allow and d.size_factor < 1.0


def test_filter_support_full_size():
    d = sentiment_filter("SELL", _sent(news.BIAS_BEARISH, news.RISK_LOW, 0.9))
    assert d.allow and d.size_factor == 1.0 and d.reason == "news_supports"


# --- store -----------------------------------------------------------------
def test_store_update_and_latest():
    payload = {"bias": "bullish", "risk_level": "medium", "confidence": 0.6,
               "event_type": "economic_data", "rationale": "強い雇用統計"}
    store = SentimentStore(analyzer=NewsAnalyzer(client=FakeAnthropic(payload)))
    assert store.latest("USD_JPY") is None
    s = store.update_from_text("USD_JPY", "強い雇用統計")
    assert store.latest("USD_JPY") is s
    assert s.bias == "bullish"


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


class _Provider:
    def __init__(self, sentiment):
        self._s = sentiment

    def latest(self, instrument):
        return self._s


def test_engine_blocks_entry_on_high_risk_news():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    up = _uptrend()
    frames = {"M15": up, "H1": up, "H4": up, "D": up}
    client = _Client()
    provider = _Provider(NewsSentiment("USD_JPY", news.BIAS_BULLISH,
                                       news.RISK_HIGH, 0.9, "central_bank", ""))
    engine = TradingEngine(s, client, feed=_Feed(frames),
                           breaker=CircuitBreaker(s), news_provider=provider)
    res = engine.run_once("2026-06-30")
    assert not client.orders
    assert res.blocked.get("USD_JPY") == "news_high_risk"


def test_engine_reduces_size_on_weak_news():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    up = _uptrend()
    frames = {"M15": up, "H1": up, "H4": up, "D": up}

    # ニュース無しの基準サイズ
    base_client = _Client()
    base_engine = TradingEngine(s, base_client, feed=_Feed(frames),
                                breaker=CircuitBreaker(s))
    base_engine.run_once("2026-06-30")
    base_units = abs(base_client.orders[0]["units"])

    # 弱い逆風ニュース → サイズ縮小
    client = _Client()
    provider = _Provider(NewsSentiment("USD_JPY", news.BIAS_BEARISH,
                                       news.RISK_LOW, 0.4, "economic_data", ""))
    engine = TradingEngine(s, client, feed=_Feed(frames),
                           breaker=CircuitBreaker(s), news_provider=provider)
    engine.run_once("2026-06-30")
    reduced_units = abs(client.orders[0]["units"])
    assert 0 < reduced_units < base_units


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
