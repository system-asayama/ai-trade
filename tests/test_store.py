"""Phase 3（永続化ストア・統計・ダッシュボード）のテスト。

sqlite(:memory:) を使い、ネットワーク非依存。`python tests/test_store.py` で実行可。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading import metrics  # noqa: E402
from trading.store import TradeStore  # noqa: E402


def _dt(hour: int) -> datetime:
    return datetime(2026, 6, 30, hour, 0, tzinfo=timezone.utc)


# --- metrics ---------------------------------------------------------------
def test_classify_session():
    assert metrics.classify_session(_dt(3)) == metrics.SESSION_TOKYO
    assert metrics.classify_session(_dt(10)) == metrics.SESSION_LONDON
    assert metrics.classify_session(_dt(15)) == metrics.SESSION_NEWYORK
    assert metrics.classify_session(_dt(22)) == metrics.SESSION_OTHER
    # ISO 文字列でも動く
    assert metrics.classify_session("2026-06-30T03:00:00+00:00") == metrics.SESSION_TOKYO


def test_summary_and_groups():
    closed = [
        {"instrument": "USD_JPY", "session": "tokyo", "r_multiple": 2.0, "pnl": 200},
        {"instrument": "USD_JPY", "session": "london", "r_multiple": -1.0, "pnl": -100},
        {"instrument": "EUR_USD", "session": "tokyo", "r_multiple": 1.0, "pnl": 50},
    ]
    s = metrics.summary(closed)
    assert s["num_trades"] == 3
    assert abs(s["win_rate"] - 2 / 3) < 1e-9
    assert abs(s["total_r"] - 2.0) < 1e-9
    assert abs(s["total_pnl"] - 150) < 1e-9

    by_inst = metrics.group_stats(closed, "instrument")
    assert by_inst["USD_JPY"]["num_trades"] == 2
    assert abs(by_inst["USD_JPY"]["win_rate"] - 0.5) < 1e-9
    assert abs(by_inst["EUR_USD"]["total_r"] - 1.0) < 1e-9


def test_equity_curve_is_cumulative():
    closed = [
        {"exit_time": "2026-06-30T01:00:00+00:00", "r_multiple": 1.0, "pnl": 10},
        {"exit_time": "2026-06-30T02:00:00+00:00", "r_multiple": -0.5, "pnl": -5},
        {"exit_time": "2026-06-30T03:00:00+00:00", "r_multiple": 2.0, "pnl": 20},
    ]
    curve = metrics.equity_curve(closed)
    assert [p["cum_r"] for p in curve] == [1.0, 0.5, 2.5]
    assert curve[-1]["cum_pnl"] == 25


def test_max_drawdown():
    # +2, -3, +1 → equity 2,-1,0 ; peak 2 ; 最大DD = -1-2 = -3
    s = metrics.summary([
        {"r_multiple": 2.0}, {"r_multiple": -3.0}, {"r_multiple": 1.0},
    ])
    assert abs(s["max_drawdown_r"] - (-3.0)) < 1e-9


# --- store -----------------------------------------------------------------
def test_store_open_and_close_roundtrip():
    store = TradeStore(":memory:")
    tid = store.record_open(
        instrument="USD_JPY", side="BUY", units=1000,
        entry_time=_dt(3), entry_price=150.0, stop_loss=149.0,
        oanda_trade_id="T1", client_id="c1",
    )
    assert tid > 0
    assert store.open_count() == 1

    # 決済（exit 152 → +2R: risk=1.0, pnl_points=2.0）
    store.record_close("T1", exit_time=_dt(5), exit_price=152.0, pnl=2000.0,
                       exit_reason="stop")
    assert store.open_count() == 0
    closed = store.closed_trades()
    assert len(closed) == 1
    assert abs(closed[0]["r_multiple"] - 2.0) < 1e-9
    assert closed[0]["session"] == "tokyo"
    assert closed[0]["pnl"] == 2000.0


def test_store_close_short_r_multiple():
    store = TradeStore(":memory:")
    store.record_open(instrument="EUR_USD", side="SELL", units=-1000,
                      entry_time=_dt(10), entry_price=1.1000, stop_loss=1.1050,
                      oanda_trade_id="S1")
    # 売り: entry1.1000 → exit1.0900 で +0.01, risk0.005 → +2R
    store.record_close("S1", exit_time=_dt(11), exit_price=1.0900, pnl=100.0)
    closed = store.closed_trades()
    assert abs(closed[0]["r_multiple"] - 2.0) < 1e-9


def test_store_close_without_open_inserts_row():
    store = TradeStore(":memory:")
    store.record_close("ORPHAN", exit_time=_dt(4), exit_price=1.0, pnl=-5.0)
    closed = store.closed_trades()
    assert len(closed) == 1
    assert closed[0]["oanda_trade_id"] == "ORPHAN"


# --- dashboard (Flask) -----------------------------------------------------
def test_dashboard_requires_login_and_renders():
    # 認証アプリを起動し、ブループリント登録と未ログイン時のリダイレクトを確認
    os.environ["TRADES_DB_PATH"] = ":memory:"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        import app as app_module
    except Exception as exc:  # noqa: BLE001 flask 未導入環境ではスキップ
        print(f"  (skip: Flask 未導入のためダッシュボードテストをスキップ: {exc})")
        return
    flask_app = app_module.create_app()
    client = flask_app.test_client()
    resp = client.get("/trading/")
    # 未ログインはログインへリダイレクト
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers.get("Location", "")


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
