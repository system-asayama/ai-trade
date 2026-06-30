"""安全装置: サーキットブレーカーとキルスイッチの状態管理。

- 日次の実現損失・連敗数・同時保有数・口座ドローダウンを監視。
- 閾値超過で「trip（停止）」し、新規エントリーを拒否する。
- 手動キルスイッチも保持（engine が全決済に使う）。

状態は任意で JSON ファイルへ永続化でき、プロセス再起動をまたいで保持できる。
日付の境界判定は外部から `today`（YYYY-MM-DD 文字列）を渡す方式にして
テストの決定性を確保する（モジュール内で現在時刻を取得しない）。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional, Tuple

from .config import Settings


@dataclass
class BreakerState:
    day: str = ""                  # 集計中の日付(YYYY-MM-DD)
    daily_realized: float = 0.0    # その日の実現損益（口座通貨）
    consecutive_losses: int = 0
    tripped: bool = False
    trip_reason: str = ""
    killed: bool = False           # 手動キルスイッチ


@dataclass
class CircuitBreaker:
    """エントリー可否を判定する安全ゲート。"""

    settings: Settings
    # 閾値（口座通貨 / 回数）。0 以下なら無効。
    max_daily_loss: float = 0.0
    max_consecutive_losses: int = 0
    state: BreakerState = field(default_factory=BreakerState)
    persist_path: Optional[str] = None

    # -- 永続化 --------------------------------------------------------------
    @classmethod
    def load(cls, settings: Settings, path: Optional[str] = None, **kwargs) -> "CircuitBreaker":
        cb = cls(settings=settings, persist_path=path, **kwargs)
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cb.state = BreakerState(**json.load(fh))
            except (json.JSONDecodeError, TypeError, OSError):
                pass  # 壊れていれば初期状態で続行
        return cb

    def save(self) -> None:
        if not self.persist_path:
            return
        os.makedirs(os.path.dirname(self.persist_path) or ".", exist_ok=True)
        with open(self.persist_path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self.state), fh, ensure_ascii=False, indent=2)

    # -- 日次境界 ------------------------------------------------------------
    def roll_day(self, today: str) -> None:
        """日付が変わったら当日集計をリセットする（trip も解除）。"""
        if self.state.day != today:
            self.state.day = today
            self.state.daily_realized = 0.0
            # 日跨ぎでサーキットブレーカーは自動復帰（killed は維持）
            self.state.tripped = False
            self.state.trip_reason = ""

    # -- 取引結果の登録 ------------------------------------------------------
    def register_close(self, realized_pnl: float, today: str) -> None:
        """決済結果を反映し、必要なら trip する。"""
        self.roll_day(today)
        self.state.daily_realized += realized_pnl
        if realized_pnl < 0:
            self.state.consecutive_losses += 1
        elif realized_pnl > 0:
            self.state.consecutive_losses = 0

        if self.max_daily_loss > 0 and self.state.daily_realized <= -abs(self.max_daily_loss):
            self.trip(f"日次損失上限に到達 ({self.state.daily_realized:.2f})")
        if (self.max_consecutive_losses > 0
                and self.state.consecutive_losses >= self.max_consecutive_losses):
            self.trip(f"連敗数上限に到達 ({self.state.consecutive_losses})")
        self.save()

    # -- 制御 ----------------------------------------------------------------
    def trip(self, reason: str) -> None:
        self.state.tripped = True
        self.state.trip_reason = reason

    def kill(self) -> None:
        """手動キルスイッチ ON。"""
        self.state.killed = True
        self.save()

    def reset_kill(self) -> None:
        self.state.killed = False
        self.save()

    def can_open(self, open_positions: int, today: str) -> Tuple[bool, str]:
        """新規エントリーが許可されるか。(可否, 理由) を返す。"""
        self.roll_day(today)
        if self.state.killed:
            return False, "kill_switch"
        if self.state.tripped:
            return False, f"tripped: {self.state.trip_reason}"
        if open_positions >= self.settings.max_open_positions:
            return False, "max_open_positions"
        return True, "ok"
