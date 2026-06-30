"""テスト/デモ用の合成 OHLCV 生成（OANDA に接続せず動かすため）。

決定論的（seed 固定）にトレンド→レンジ→逆トレンドの価格系列を作る。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_ohlcv(
    n_bars: int = 3000,
    start_price: float = 150.0,
    granularity_minutes: int = 15,
    seed: int = 7,
) -> pd.DataFrame:
    """M15 想定の合成ローソク足を生成する。

    緩やかな上昇トレンド → レンジ → 下降トレンド を含むので、
    MTF一致ブレイク戦略のシグナルが発生しうる。
    """
    rng = np.random.default_rng(seed)

    # ドリフト（区間ごとに方向を変える）
    third = n_bars // 3
    drift = np.concatenate(
        [
            np.full(third, 0.015),          # 上昇
            np.full(third, 0.0),            # レンジ
            np.full(n_bars - 2 * third, -0.015),  # 下降
        ]
    )
    noise = rng.normal(0.0, 0.05, size=n_bars)
    returns = drift + noise

    close = start_price + np.cumsum(returns)
    open_ = np.empty(n_bars)
    open_[0] = start_price
    open_[1:] = close[:-1]

    # 高値/安値はバー内レンジを付与
    bar_range = np.abs(rng.normal(0.04, 0.02, size=n_bars)) + 0.01
    high = np.maximum(open_, close) + bar_range
    low = np.minimum(open_, close) - bar_range
    volume = rng.integers(80, 400, size=n_bars).astype(float)

    index = pd.date_range(
        "2024-01-01", periods=n_bars, freq=f"{granularity_minutes}min", tz="UTC"
    )
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )
