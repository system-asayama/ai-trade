#!/usr/bin/env python3
"""ライブ取引エンジンの起動・キルスイッチ操作 CLI（Phase 2）。

既定は OANDA practice（デモ口座）。実弁(live)は OANDA_ENV=live を明示し、
かつ十分なフォワードテストを経た場合のみ使用すること。

使い方:
    # 1ティックだけ実行（動作確認）
    OANDA_API_TOKEN=... OANDA_ACCOUNT_ID=... python scripts/run_engine.py --once

    # 常駐（60秒間隔）
    python scripts/run_engine.py --poll 60

    # キルスイッチ ON（全建玉決済 + 新規停止） / 解除 / 状態表示
    python scripts/run_engine.py --kill
    python scripts/run_engine.py --reset-kill
    python scripts/run_engine.py --status
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.config import Settings  # noqa: E402
from trading.engine import TradingEngine  # noqa: E402
from trading.oanda_client import OandaClient  # noqa: E402
from trading.safety import CircuitBreaker  # noqa: E402
from trading.store import TradeStore  # noqa: E402

BREAKER_STATE_PATH = os.environ.get("BREAKER_STATE_PATH", "instance/breaker.json")


def build_engine(settings: Settings):
    client = OandaClient(settings)
    breaker = CircuitBreaker.load(
        settings,
        path=BREAKER_STATE_PATH,
        max_daily_loss=float(os.environ.get("MAX_DAILY_LOSS", "0") or 0),
        max_consecutive_losses=int(os.environ.get("MAX_CONSECUTIVE_LOSSES", "0") or 0),
    )
    engine = TradingEngine(settings, client, breaker=breaker, store=TradeStore())
    return engine, breaker


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="AI FX ライブエンジン")
    parser.add_argument("--once", action="store_true", help="1ティックだけ実行")
    parser.add_argument("--poll", type=int, default=60, help="常駐時のポーリング秒")
    parser.add_argument("--kill", action="store_true", help="キルスイッチON（全決済+停止）")
    parser.add_argument("--reset-kill", action="store_true", help="キルスイッチ解除")
    parser.add_argument("--status", action="store_true", help="ブレーカー状態を表示")
    args = parser.parse_args()

    settings = Settings()
    print(f"OANDA_ENV={settings.oanda_env} instruments={settings.instruments}")

    engine, breaker = build_engine(settings)

    if args.status:
        print("breaker state:", breaker.state)
        return 0
    if args.kill:
        breaker.kill()
        engine.run_once(time.strftime("%Y-%m-%d", time.gmtime()))
        print("キルスイッチON。全建玉を決済し、新規エントリーを停止しました。")
        return 0
    if args.reset_kill:
        breaker.reset_kill()
        print("キルスイッチを解除しました。")
        return 0

    if args.once:
        res = engine.run_once(time.strftime("%Y-%m-%d", time.gmtime()))
        print("tick result:", res)
        return 0

    print(f"常駐開始（{args.poll}秒間隔）。Ctrl+C で停止。")
    try:
        engine.run_forever(poll_seconds=args.poll)
    except KeyboardInterrupt:
        print("\n停止しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
