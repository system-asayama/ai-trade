#!/usr/bin/env python3
"""HistData.com の長期M1データを取り込むCLI（長期バックテスト用）。

同じ15分足ロジックのまま数年ぶんを検証できるよう、M1を取り込み→M15集約→保存する。

使い方:
    # 自動ダウンロード（ベストエフォート）: USD_JPY を 2022〜2025 年
    python scripts/import_histdata.py USD_JPY --years 2022-2025

    # 手動DLしたファイルを取り込む（確実）
    python scripts/import_histdata.py USD_JPY --files DAT_ASCII_USDJPY_M1_2024.zip

取り込み後、ブラウザのバックテストで「長期（取り込み済み）」を選べます。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.histdata import (  # noqa: E402
    HistStore,
    download_year,
    import_m1_bytes,
)


def _parse_years(spec: str):
    if "-" in spec:
        a, b = spec.split("-", 1)
        return range(int(a), int(b) + 1)
    return [int(spec)]


def main() -> int:
    parser = argparse.ArgumentParser(description="HistData 長期データ取り込み")
    parser.add_argument("instrument", help="例: USD_JPY")
    parser.add_argument("--years", help="例: 2022-2025 または 2024")
    parser.add_argument("--files", nargs="*", help="手動DLした zip/csv のパス")
    args = parser.parse_args()

    store = HistStore()
    total = 0

    if args.files:
        for path in args.files:
            with open(path, "rb") as fh:
                data = fh.read()
            n = import_m1_bytes(store, args.instrument, data,
                                is_zip=path.lower().endswith(".zip"))
            print(f"取り込み {path}: {n} 本(M15)")
            total += n
    elif args.years:
        for year in _parse_years(args.years):
            try:
                data = download_year(args.instrument, year)
                n = import_m1_bytes(store, args.instrument, data, is_zip=True)
                print(f"取り込み {year}: {n} 本(M15)")
                total += n
            except Exception as exc:  # noqa: BLE001
                print(f"{year} のダウンロード失敗: {exc}")
                print("  → histdata.com から手動DLし、--files で取り込んでください。")
    else:
        print("--years か --files のどちらかを指定してください。")
        return 2

    mn, mx, cnt = store.coverage(args.instrument, "M15")
    print(f"\n保存済み {args.instrument}(M15): {cnt} 本  期間 {mn} 〜 {mx}  (今回 +{total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
