"""経済指標カレンダー（危険度フィルタ）。

高重要度の経済指標・イベントの前後は急変リスクが高いため、対象通貨の
高重要度イベントが「前 before 分 〜 後 after 分」の窓に入る場合は
新規エントリーを見送る（ブラックアウト）。

- プロバイダは差し替え可能（Static / HTTP）。HTTP は requests を注入可能に
  してネットワーク非依存でテストできる。
- 時刻はすべて UTC の tz-aware datetime に正規化する。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:  # HTTP プロバイダ利用時のみ必要
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .config import Settings

IMPORTANCE_LOW = "low"
IMPORTANCE_MEDIUM = "medium"
IMPORTANCE_HIGH = "high"
_RANK = {IMPORTANCE_LOW: 1, IMPORTANCE_MEDIUM: 2, IMPORTANCE_HIGH: 3}


def importance_rank(importance: str) -> int:
    return _RANK.get(str(importance).lower(), 0)


def normalize_importance(value: Any) -> str:
    """各種表現（High/3/"3"/★★★ 等）を low/medium/high に正規化する。"""
    if value is None:
        return IMPORTANCE_LOW
    text = str(value).strip().lower()
    if text in _RANK:
        return text
    # 数値（1/2/3 や ★の数）
    try:
        num = int(float(text)) if text.replace(".", "", 1).isdigit() else text.count("★") or text.count("*")
    except ValueError:
        num = 0
    if num >= 3:
        return IMPORTANCE_HIGH
    if num == 2:
        return IMPORTANCE_MEDIUM
    if num == 1:
        return IMPORTANCE_LOW
    # キーワード
    if "high" in text or "高" in text:
        return IMPORTANCE_HIGH
    if "med" in text or "中" in text:
        return IMPORTANCE_MEDIUM
    return IMPORTANCE_LOW


def parse_time(value: Any) -> Optional[datetime]:
    """ISO8601 / epoch 秒 を UTC tz-aware datetime に変換する。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():  # epoch
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class EconomicEvent:
    time: datetime          # UTC tz-aware
    currency: str
    importance: str         # low / medium / high
    title: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any], field_map: Dict[str, str]) -> Optional["EconomicEvent"]:
        when = parse_time(data.get(field_map.get("time", "time")))
        currency = data.get(field_map.get("currency", "currency"))
        if when is None or not currency:
            return None
        return cls(
            time=when,
            currency=str(currency).upper(),
            importance=normalize_importance(data.get(field_map.get("importance", "importance"))),
            title=str(data.get(field_map.get("title", "title"), "")),
        )


# --- プロバイダ ------------------------------------------------------------
class StaticCalendarProvider:
    """与えられたイベント一覧をそのまま返す（テスト/手動投入用）。"""

    def __init__(self, events: List[EconomicEvent]) -> None:
        self._events = list(events)

    def fetch_events(self) -> List[EconomicEvent]:
        return list(self._events)


# よくある JSON カレンダーのデフォルトキー対応
DEFAULT_FIELD_MAP = {
    "time": "date",
    "currency": "country",
    "importance": "impact",
    "title": "title",
}


class HttpCalendarProvider:
    """汎用 HTTP JSON カレンダー。フィールド名は field_map で調整する。

    多くの無料/有料カレンダーは「イベントの配列」を返す。配列が
    トップレベルでない場合は list_key でネストを指定する。
    """

    def __init__(
        self,
        url: str,
        field_map: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        list_key: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.url = url
        self.field_map = field_map or DEFAULT_FIELD_MAP
        self.params = params or {}
        self.headers = headers or {}
        self.list_key = list_key
        if session is not None:
            self._session = session
        elif requests is not None:
            self._session = requests.Session()
        else:  # pragma: no cover
            self._session = None

    def fetch_events(self) -> List[EconomicEvent]:
        if self._session is None:  # pragma: no cover
            raise RuntimeError("requests が利用できません。")
        resp = self._session.get(self.url, params=self.params,
                                 headers=self.headers, timeout=30)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        data = resp.json()
        items = data.get(self.list_key, []) if (self.list_key and isinstance(data, dict)) else data
        events = []
        for item in items or []:
            ev = EconomicEvent.from_dict(item, self.field_map)
            if ev is not None:
                events.append(ev)
        return events


# --- カレンダー本体 --------------------------------------------------------
class EconomicCalendar:
    def __init__(self, provider: Any, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings
        self.events: List[EconomicEvent] = []
        self._last_monotonic: Optional[float] = None

    def refresh(self) -> int:
        """プロバイダから最新イベントを取得してキャッシュする。件数を返す。"""
        self.events = sorted(self.provider.fetch_events(), key=lambda e: e.time)
        self._last_monotonic = time.monotonic()
        return len(self.events)

    def maybe_refresh(self, ttl_seconds: int = 900) -> None:
        """前回更新から ttl 以上経過していれば再取得する（失敗は無視）。"""
        now = time.monotonic()
        if self._last_monotonic is not None and (now - self._last_monotonic) < ttl_seconds:
            return
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001 取得失敗時は前回のキャッシュで続行
            logging.getLogger("trading.calendar").warning("カレンダー更新失敗: %s", exc)

    def is_blackout(
        self, instrument: str, when: datetime,
        importance_min: Optional[str] = None,
    ) -> Tuple[bool, Optional[EconomicEvent]]:
        """when が対象通貨の高重要度イベント前後の窓に入るか判定する。"""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        currencies = {c.upper() for c in instrument.split("_") if c}
        min_rank = importance_rank(importance_min or self.settings.econ_importance_min)
        before = timedelta(minutes=self.settings.econ_blackout_before_min)
        after = timedelta(minutes=self.settings.econ_blackout_after_min)

        for ev in self.events:
            if ev.currency not in currencies:
                continue
            if importance_rank(ev.importance) < min_rank:
                continue
            if ev.time - before <= when <= ev.time + after:
                return True, ev
        return False, None
