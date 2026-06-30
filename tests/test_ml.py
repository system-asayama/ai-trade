"""Phase 5（ダマシ予測ML・チャート画像認識・AI合議）のテスト。

ML は合成データ、Claude系は Fake クライアントでネットワーク非依存。
`python tests/test_ml.py` で実行可能。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from trading import ml  # noqa: E402
from trading.config import Settings  # noqa: E402
from trading.council import Council, VOTE_SKIP, VOTE_TRADE  # noqa: E402
from trading.engine import TradingEngine  # noqa: E402
from trading.ml import FakeoutModel, build_training_set, features_from_reason  # noqa: E402
from trading.safety import CircuitBreaker  # noqa: E402
from trading.vision import ChartAnalyzer, render_chart  # noqa: E402


def settings(**env):
    defaults = {"EMA_SLOW": "100", "INSTRUMENTS": "USD_JPY"}
    defaults.update({k: str(v) for k, v in env.items()})
    for k, v in defaults.items():
        os.environ[k] = v
    return Settings()


# --- Fake Anthropic --------------------------------------------------------
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, payload):
        self.content = [_Block(json.dumps(payload))]


class FakeAnthropic:
    """create() 呼び出し毎に payloads を順に返す。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

        class _M:
            def create(inner, **kwargs):
                p = self._payloads[min(self.calls, len(self._payloads) - 1)]
                self.calls += 1
                return _Resp(p)
        self.messages = _M()


# --- ML --------------------------------------------------------------------
def test_features_from_reason_order():
    reason = {"atr_pct": 0.8, "adx": 50.0, "volume_ratio": 1.5,
              "breakout": "up", "mtf": "up"}
    f = features_from_reason(reason)
    assert list(f) == [0.8, 0.5, 1.5, 1.0, 1.0]


def test_model_learns_separable_data():
    rng = np.random.default_rng(0)
    # クラス1（成功）は adx 高め、クラス0（ダマシ）は adx 低め
    n = 200
    x1 = np.column_stack([rng.uniform(0.5, 1, n), rng.uniform(0.5, 1, n),
                          rng.uniform(1, 2, n), np.ones(n), np.ones(n)])
    x0 = np.column_stack([rng.uniform(0, 0.5, n), rng.uniform(0, 0.3, n),
                          rng.uniform(0.5, 1, n), np.ones(n), np.ones(n)])
    X = np.vstack([x1, x0])
    y = np.concatenate([np.ones(n), np.zeros(n)])
    model = FakeoutModel().fit(X, y)
    # 学習後、高adxサンプルは高確率、低adxは低確率
    assert model.predict_proba([0.9, 0.9, 1.8, 1, 1]) > 0.7
    assert model.predict_proba([0.1, 0.1, 0.6, 1, 1]) < 0.3


def test_model_untrained_returns_neutral():
    assert FakeoutModel().predict_proba([0.5, 0.5, 1.0, 1, 1]) == 0.5


def test_model_save_load_roundtrip(tmp="/tmp/claude-0/fakeout.json"):
    X = np.random.default_rng(1).uniform(0, 1, (50, 5))
    y = (X[:, 1] > 0.5).astype(float)
    m = FakeoutModel().fit(X, y)
    m.save(tmp)
    loaded = FakeoutModel.load(tmp)
    x = [0.5, 0.9, 1.0, 1, 1]
    assert abs(loaded.predict_proba(x) - m.predict_proba(x)) < 1e-9
    os.remove(tmp)


def test_build_training_set_from_store_rows():
    rows = [
        {"entry_features": json.dumps({"atr_pct": 0.8, "adx": 40, "volume_ratio": 1.4,
                                       "breakout": "up", "mtf": "up"}), "r_multiple": 2.0},
        {"entry_features": json.dumps({"atr_pct": 0.2, "adx": 10, "volume_ratio": 0.9,
                                       "breakout": "up", "mtf": "up"}), "r_multiple": -1.0},
        {"entry_features": None, "r_multiple": 1.0},  # スキップされる
    ]
    X, y = build_training_set(rows)
    assert X.shape == (2, 5)
    assert list(y) == [1.0, 0.0]


# --- vision ----------------------------------------------------------------
def test_chart_analyzer_parses():
    payload = {"trend": "up", "pattern": "ascending triangle", "fakeout_risk": "low",
               "confidence": 0.7, "rationale": "高値切り上げ"}
    client = FakeAnthropic([payload])
    read = ChartAnalyzer(client=client).analyze("USD_JPY", b"\x89PNG fake")
    assert read.trend == "up"
    assert read.fakeout_risk == "low"
    assert abs(read.confidence - 0.7) < 1e-9


def test_render_chart_returns_png_or_none():
    df = pd.DataFrame({
        "open": [1, 2, 3], "high": [1.5, 2.5, 3.5],
        "low": [0.5, 1.5, 2.5], "close": [1.2, 2.2, 3.2], "volume": [10, 20, 30]})
    out = render_chart(df, "test")
    # matplotlib が無い環境では None、あれば PNG バイト列
    assert out is None or (isinstance(out, bytes) and out[:4] == b"\x89PNG")


# --- council ---------------------------------------------------------------
def test_council_majority_trade_allows():
    # technical/macro=trade, risk=skip → 2/3 → 許可
    client = FakeAnthropic([
        {"vote": VOTE_TRADE, "confidence": 0.8, "rationale": "a"},
        {"vote": VOTE_TRADE, "confidence": 0.7, "rationale": "b"},
        {"vote": VOTE_SKIP, "confidence": 0.6, "rationale": "c"},
    ])
    v = Council(client=client).evaluate("USD_JPY", "BUY", "ctx")
    assert v.allow
    assert v.trade_votes == 2
    assert 0.5 <= v.size_factor <= 1.0
    assert client.calls == 3


def test_council_majority_skip_blocks():
    client = FakeAnthropic([
        {"vote": VOTE_SKIP, "confidence": 0.8, "rationale": "a"},
        {"vote": VOTE_SKIP, "confidence": 0.7, "rationale": "b"},
        {"vote": VOTE_TRADE, "confidence": 0.6, "rationale": "c"},
    ])
    v = Council(client=client).evaluate("USD_JPY", "BUY", "ctx")
    assert not v.allow and v.size_factor == 0.0


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


class _StubModel:
    def __init__(self, p):
        self.p = p

    def predict_proba(self, x):
        return self.p


def test_engine_ml_gate_blocks_low_probability():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    frames = {"M15": _uptrend(), "H1": _uptrend(), "H4": _uptrend(), "D": _uptrend()}
    client = _Client()
    engine = TradingEngine(s, client, feed=_Feed(frames),
                           breaker=CircuitBreaker(s), fakeout_model=_StubModel(0.2))
    res = engine.run_once("2026-06-30")
    assert not client.orders
    assert res.blocked.get("USD_JPY", "").startswith("fakeout_risk")


def test_engine_ml_gate_allows_high_probability():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    frames = {"M15": _uptrend(), "H1": _uptrend(), "H4": _uptrend(), "D": _uptrend()}
    client = _Client()
    engine = TradingEngine(s, client, feed=_Feed(frames),
                           breaker=CircuitBreaker(s), fakeout_model=_StubModel(0.9))
    engine.run_once("2026-06-30")
    assert client.orders  # 高確率なら発注される


def test_engine_council_gate_blocks_on_skip_majority():
    s = settings(INSTRUMENTS="USD_JPY", MAX_OPEN_POSITIONS=2)
    frames = {"M15": _uptrend(), "H1": _uptrend(), "H4": _uptrend(), "D": _uptrend()}
    client = _Client()
    council = Council(client=FakeAnthropic([
        {"vote": VOTE_SKIP, "confidence": 0.8, "rationale": "a"},
        {"vote": VOTE_SKIP, "confidence": 0.8, "rationale": "b"},
        {"vote": VOTE_SKIP, "confidence": 0.8, "rationale": "c"},
    ]))
    engine = TradingEngine(s, client, feed=_Feed(frames),
                           breaker=CircuitBreaker(s), council=council)
    res = engine.run_once("2026-06-30")
    assert not client.orders
    assert res.blocked.get("USD_JPY", "").startswith("council_")


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
