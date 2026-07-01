#!/usr/bin/env python3
"""マルチテナント常駐ランナー（各ユーザーが持ち込んだ設定で自動売買）。

engine_enabled かつ OANDA トークンが設定されたユーザーごとに、
その人の設定・APIキーでエンジンを1ティック実行する。常駐ループ対応。

使い方:
    python scripts/run_multi.py --once     # 全ユーザーを1回実行
    python scripts/run_multi.py --poll 60  # 常駐（60秒間隔）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("run_multi")


def _enabled_settings(app_module):
    """engine_enabled かつトークンありのユーザー設定を返す。"""
    from models import UserSettings
    out = []
    with app_module.app.app_context():
        for us in UserSettings.query.filter_by(engine_enabled=True).all():
            if us.has_oanda_token:
                out.append(us)
    return out


def run_all_once(app_module) -> int:
    from trading.tenant import build_user_engine
    today = time.strftime("%Y-%m-%d", time.gmtime())
    ran = 0
    for us in _enabled_settings(app_module):
        try:
            engine = build_user_engine(us)
            res = engine.run_once(today)
            logger.info("user=%s entries=%s blocked=%s",
                        us.user_id, res.entries, res.blocked)
            ran += 1
        except Exception as exc:  # noqa: BLE001 個別ユーザーの失敗で全体を止めない
            logger.exception("user=%s の実行に失敗: %s", us.user_id, exc)
    return ran


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="マルチテナント自動売買ランナー")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll", type=int, default=60)
    args = parser.parse_args()

    import app as app_module

    if args.once:
        n = run_all_once(app_module)
        print(f"{n} 名のユーザーを実行しました。")
        return 0

    print(f"マルチテナント常駐開始（{args.poll}秒間隔）。Ctrl+C で停止。")
    try:
        while True:
            run_all_once(app_module)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n停止しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
