"""HistData 長期データ取り込みのテスト（ネットワーク非依存）。

`python tests/test_histdata.py` で実行可能。
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from trading import histdata as hd  # noqa: E402
from trading.backtester import Backtester  # noqa: E402
from trading.config import Settings  # noqa: E402
from trading.data_feed import candles_to_df  # noqa: E402
from trading.histdata import HistStore, import_m1_bytes, parse_m1_text, to_m15, to_pair  # noqa: E402


def test_to_pair():
    assert to_pair("USD_JPY") == "usdjpy"
    assert to_pair("EUR_USD") == "eurusd"


def test_parse_m1_est_to_utc():
    text = "20240101 000000;150.00;150.20;149.90;150.10;0\n" \
           "20240101 000100;150.10;150.30;150.05;150.25;0\n"
    df = parse_m1_text(text)
    assert len(df) == 2
    # EST(UTC-5) → UTC は +5時間
    assert df.index[0] == datetime(2024, 1, 1, 5, 0, tzinfo=timezone.utc)
    assert df["open"].iloc[0] == 150.00 and df["close"].iloc[1] == 150.25


def test_parse_skips_bad_lines():
    text = "bad line\n20240101 000000;1;2;0.5;1.5;0\n;;;\n"
    assert len(parse_m1_text(text)) == 1


def test_parse_zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("DAT_ASCII_USDJPY_M1_2024.csv",
                    "20240101 000000;150.0;150.2;149.9;150.1;0\n")
    df = hd.parse_zip_bytes(buf.getvalue())
    assert len(df) == 1 and df["close"].iloc[0] == 150.1


def _m1_df(n=3000, step=0.002):
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    close = 150.0 + np.arange(n) * step
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + 0.005,
                         "low": np.minimum(open_, close) - 0.005, "close": close,
                         "volume": np.zeros(n)}, index=idx)


def test_to_m15_aggregates():
    m1 = _m1_df(60)  # 60分 → 概ね4本のM15（境界で端数バーが出ることがある）
    m15 = to_m15(m1)
    assert 4 <= len(m15) <= 5
    # OHLC の整合性（高値>=安値、高値>=始値/終値）が保たれている
    assert (m15["high"] >= m15["low"]).all()
    assert (m15["high"] >= m15["close"]).all()
    assert (m15["low"] <= m15["open"]).all()
    # 集約後の高値は元M1の高値の範囲に収まる
    assert m15["high"].max() <= m1["high"].max()


def test_store_roundtrip_and_coverage():
    store = HistStore(":memory:")
    m15 = to_m15(_m1_df(3000))
    n = store.import_df("USD_JPY", "M15", m15)
    assert n == len(m15)
    mn, mx, cnt = store.coverage("USD_JPY", "M15")
    assert cnt == len(m15)
    assert store.instruments() == ["USD_JPY"]
    candles = store.load_candles("USD_JPY", "M15")
    assert len(candles) == len(m15)
    assert candles[0]["complete"] is True
    # OANDA互換で DataFrame 化できる
    assert len(candles_to_df(candles)) == len(m15)


def test_store_limit_returns_latest():
    store = HistStore(":memory:")
    store.import_df("USD_JPY", "M15", to_m15(_m1_df(3000)))
    latest = store.load_candles("USD_JPY", "M15", limit=10)
    assert len(latest) == 10


def test_year_count():
    store = HistStore(":memory:")
    m15 = to_m15(_m1_df(3000))  # 2024-01-01 起点
    store.import_df("USD_JPY", "M15", m15)
    assert store.year_count("USD_JPY", 2024) == len(m15)  # 全て2024年
    assert store.year_count("USD_JPY", 2023) == 0         # 別年は0
    assert store.year_count("EUR_USD", 2024) == 0         # 別ペアは0


def test_import_m1_bytes_csv():
    store = HistStore(":memory:")
    text = "\n".join(
        f"202401{d:02d} 00{m:02d}00;150.{d};150.{d}5;150.{d};150.{d}2;0"
        for d in range(1, 3) for m in range(0, 60)) + "\n"
    n = import_m1_bytes(store, "USD_JPY", text.encode(), is_zip=False)
    assert n > 0
    assert store.coverage("USD_JPY", "M15")[2] == n


def test_backtest_over_imported_data_runs():
    os.environ["EMA_SLOW"] = "100"
    store = HistStore(":memory:")
    store.import_df("USD_JPY", "M15", to_m15(_m1_df(6000)))
    candles = store.load_candles("USD_JPY", "M15")
    df = candles_to_df(candles)
    result = Backtester(Settings(), spread_pips=0.8).run("USD_JPY", df)
    assert np.isfinite(result.total_r)
    assert result.num_trades >= 0


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
