#!/usr/bin/env python3
"""バックテストのデモ実行スクリプト。

- OANDA_API_TOKEN が設定されていれば practice 口座から実データを取得。
- 未設定なら合成データ（trading.synthetic）で動作確認する。

使い方:
    python scripts/run_backtest.py                 # 合成データ（オフライン）
    OANDA_API_TOKEN=... OANDA_ACCOUNT_ID=... \
        python scripts/run_backtest.py --live-data # OANDA practice から取得
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.backtester import Backtester  # noqa: E402
from trading.config import Settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AI FX バックテスト デモ")
    parser.add_argument("--live-data", action="store_true",
                        help="OANDA practice から実データを取得する")
    parser.add_argument("--count", type=int, default=3000,
                        help="取得/生成するトリガー足の本数")
    args = parser.parse_args()

    settings = Settings()

    if args.live_data:
        from trading.data_feed import DataFeed

        feed = DataFeed(settings)
        print(f"OANDA({settings.oanda_env}) からデータ取得中 ...")
        datasets = {
            inst: feed.fetch(inst, settings.trigger_granularity, count=args.count)
            for inst in settings.instruments
        }
    else:
        from trading.synthetic import make_ohlcv

        print("合成データでバックテスト（オフライン）")
        datasets = {inst: make_ohlcv(args.count, seed=i + 1)
                    for i, inst in enumerate(settings.instruments)}

    bt = Backtester(settings)
    print(f"\n{'instrument':12} {'trades':>7} {'win%':>7} {'totalR':>9} "
          f"{'expR':>8} {'maxDD_R':>9}")
    print("-" * 56)
    for inst, df in datasets.items():
        if df.empty:
            print(f"{inst:12} (データ無し)")
            continue
        res = bt.run(inst, df)
        s = res.summary()
        print(f"{inst:12} {s['num_trades']:>7} {s['win_rate']*100:>6.1f}% "
              f"{s['total_r']:>9.2f} {s['expectancy_r']:>8.2f} {s['max_drawdown_r']:>9.2f}")
    print("\n注: R倍数はインストルメント非依存の指標。合成データの結果は実相場の"
          "成績を意味しません（過学習・スリッページ・スプレッド未考慮）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
