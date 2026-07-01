"""統計集計（Phase 3）: 勝率・期待値・エクイティ曲線・セッション分類。

ストアから取り出した取引行（dict のリスト）に対して純粋に集計する。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

# セッション（UTC時刻ベースの簡易区分。実際の市場は重複するため近似）
SESSION_TOKYO = "tokyo"
SESSION_LONDON = "london"
SESSION_NEWYORK = "newyork"
SESSION_OTHER = "other"


def classify_session(entry_time: Any) -> str:
    """エントリー時刻(UTC)から取引セッションを推定する。"""
    hour = _hour_of(entry_time)
    if hour is None:
        return SESSION_OTHER
    if 0 <= hour < 8:
        return SESSION_TOKYO
    if 8 <= hour < 13:
        return SESSION_LONDON
    if 13 <= hour < 21:
        return SESSION_NEWYORK
    return SESSION_OTHER


def _hour_of(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.hour
    if hasattr(value, "hour"):  # pandas.Timestamp
        return int(value.hour)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).hour
        except ValueError:
            return None
    return None


def summary(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """全体サマリ。closed は決済済み取引行のリスト。"""
    n = len(closed)
    if n == 0:
        return {"num_trades": 0, "win_rate": 0.0, "total_pnl": 0.0,
                "total_r": 0.0, "expectancy_r": 0.0, "max_drawdown_r": 0.0}

    rs = [_f(t.get("r_multiple")) for t in closed]
    pnls = [_f(t.get("pnl")) for t in closed]
    wins = sum(1 for r in rs if r > 0)
    total_r = sum(rs)
    return {
        "num_trades": n,
        "win_rate": wins / n,
        "total_pnl": sum(pnls),
        "total_r": total_r,
        "expectancy_r": total_r / n,
        "max_drawdown_r": _max_drawdown([_f(t.get("r_multiple")) for t in closed]),
    }


def group_stats(closed: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    """key（'instrument' / 'session' など）でグルーピングした勝率・期待値。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in closed:
        groups.setdefault(str(t.get(key) or "unknown"), []).append(t)
    out: Dict[str, Dict[str, Any]] = {}
    for name, items in sorted(groups.items()):
        rs = [_f(t.get("r_multiple")) for t in items]
        wins = sum(1 for r in rs if r > 0)
        out[name] = {
            "num_trades": len(items),
            "win_rate": wins / len(items) if items else 0.0,
            "total_r": sum(rs),
            "expectancy_r": sum(rs) / len(items) if items else 0.0,
        }
    return out


def equity_curve(closed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """決済時刻順の累積損益(R / pnl)の系列。"""
    ordered = sorted(closed, key=lambda t: t.get("exit_time") or "")
    cum_r = 0.0
    cum_pnl = 0.0
    points = []
    for t in ordered:
        cum_r += _f(t.get("r_multiple"))
        cum_pnl += _f(t.get("pnl"))
        points.append({
            "time": t.get("exit_time"),
            "cum_r": round(cum_r, 4),
            "cum_pnl": round(cum_pnl, 4),
        })
    return points


def _f(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _max_drawdown(r_series: List[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_series:
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return max_dd
