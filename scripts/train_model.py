#!/usr/bin/env python3
"""ダマシ予測モデルを取引ログから学習する CLI（Phase 5）。

store に蓄積された決済済み取引の entry_features と勝敗(r_multiple)から
ロジスティック回帰を学習し、JSON に保存する。

使い方:
    python scripts/train_model.py
    # 学習済みモデルは FAKEOUT_MODEL_PATH（既定 instance/fakeout_model.json）へ
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.ml import FakeoutModel, build_training_set  # noqa: E402
from trading.store import TradeStore  # noqa: E402

MODEL_PATH = os.environ.get("FAKEOUT_MODEL_PATH", "instance/fakeout_model.json")
MIN_SAMPLES = int(os.environ.get("ML_MIN_SAMPLES", "30"))


def main() -> int:
    store = TradeStore()
    X, y = build_training_set(store.closed_trades())
    n = len(y)
    print(f"学習サンプル数: {n}（うち勝ち {int(y.sum()) if n else 0}）")
    if n < MIN_SAMPLES:
        print(f"サンプルが不足しています（最低 {MIN_SAMPLES} 件）。"
              "デモ運用で取引を蓄積してから再実行してください。")
        return 1

    model = FakeoutModel().fit(X, y)
    model.save(MODEL_PATH)
    print(f"モデルを保存しました: {MODEL_PATH}")
    # 学習データ上の簡易精度
    preds = [1.0 if model.predict_proba(x) >= 0.5 else 0.0 for x in X]
    acc = sum(int(p == t) for p, t in zip(preds, y)) / n
    print(f"学習データ精度: {acc:.1%}（参考値。過学習に注意）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
