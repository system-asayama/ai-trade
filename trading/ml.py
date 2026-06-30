"""ダマシブレイク確率予測（Phase 5, 軽量ML）。

scikit-learn 等に依存せず、純 numpy のロジスティック回帰で実装する
（インストール安定性・テスト決定性のため）。ラベル 1 = 勝ち（ブレイク成功）、
0 = 負け（ダマシ）。predict_proba は「成功確率」を返す。

特徴量は strategy.evaluate が signal.reason に格納する値から組み立てる。
これにより、ライブ判定と取引ログからの学習が同じ特徴量で一貫する。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# 特徴量の順序（features_from_reason と一致させること）
FEATURE_NAMES = ["atr_pct", "adx", "volume_ratio", "breakout_dir", "mtf_dir"]

_DIR = {"up": 1.0, "down": -1.0}


def features_from_reason(reason: Dict[str, Any]) -> np.ndarray:
    """signal.reason / 保存済み entry_features から特徴量ベクトルを作る。"""
    atr_pct = reason.get("atr_pct")
    atr_pct = float(atr_pct) if atr_pct is not None else 0.5
    adx = float(reason.get("adx", 0.0) or 0.0) / 100.0
    vol = float(reason.get("volume_ratio", 1.0) or 1.0)
    brk = _DIR.get(reason.get("breakout"), 0.0)
    mtf = _DIR.get(reason.get("mtf"), 0.0)
    return np.array([atr_pct, adx, vol, brk, mtf], dtype=float)


class FakeoutModel:
    """ロジスティック回帰（標準化 + 勾配降下）。"""

    def __init__(
        self,
        weights: Optional[np.ndarray] = None,
        bias: float = 0.0,
        mean: Optional[np.ndarray] = None,
        std: Optional[np.ndarray] = None,
    ) -> None:
        self.weights = weights
        self.bias = bias
        self.mean = mean
        self.std = std

    @property
    def is_trained(self) -> bool:
        return self.weights is not None

    @staticmethod
    def _sigmoid(z: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def fit(self, X: np.ndarray, y: np.ndarray, epochs: int = 800,
            lr: float = 0.3, l2: float = 1e-4) -> "FakeoutModel":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0)
        self.std[self.std == 0] = 1.0  # 定数列の 0除算回避
        Xs = (X - self.mean) / self.std

        n, d = Xs.shape
        w = np.zeros(d)
        b = 0.0
        for _ in range(epochs):
            p = self._sigmoid(Xs @ w + b)
            grad_w = Xs.T @ (p - y) / n + l2 * w
            grad_b = float(np.mean(p - y))
            w -= lr * grad_w
            b -= lr * grad_b
        self.weights = w
        self.bias = b
        return self

    def predict_proba(self, x: np.ndarray) -> float:
        """成功確率を返す。未学習なら中立 0.5。"""
        if not self.is_trained:
            return 0.5
        x = np.asarray(x, dtype=float)
        xs = (x - self.mean) / self.std
        return float(self._sigmoid(xs @ self.weights + self.bias))

    # -- 永続化 --------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "weights": None if self.weights is None else self.weights.tolist(),
            "bias": self.bias,
            "mean": None if self.mean is None else self.mean.tolist(),
            "std": None if self.std is None else self.std.tolist(),
            "feature_names": FEATURE_NAMES,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FakeoutModel":
        def arr(v):
            return None if v is None else np.array(v, dtype=float)
        return cls(arr(data.get("weights")), float(data.get("bias", 0.0)),
                   arr(data.get("mean")), arr(data.get("std")))

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "FakeoutModel":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def build_training_set(closed_trades: List[Dict[str, Any]]) -> Tuple[np.ndarray, np.ndarray]:
    """store.closed_trades() から (X, y) を組み立てる。

    entry_features(JSON) を特徴量へ、r_multiple>0 を勝ち(1) とする。
    """
    rows: List[np.ndarray] = []
    labels: List[float] = []
    for t in closed_trades:
        raw = t.get("entry_features")
        if not raw:
            continue
        try:
            reason = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        r = t.get("r_multiple")
        if r is None:
            continue
        rows.append(features_from_reason(reason))
        labels.append(1.0 if float(r) > 0 else 0.0)
    if not rows:
        return np.empty((0, len(FEATURE_NAMES))), np.empty((0,))
    return np.vstack(rows), np.array(labels, dtype=float)
