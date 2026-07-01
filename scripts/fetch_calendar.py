#!/usr/bin/env python3
"""経済指標カレンダーを取得して表示する CLI。

環境変数:
  ECON_CALENDAR_URL       取得先 JSON エンドポイント（必須）
  ECON_CALENDAR_FIELD_*   フィールド名対応（任意）:
                          ECON_CALENDAR_FIELD_TIME / _CURRENCY / _IMPORTANCE / _TITLE
  ECON_CALENDAR_LIST_KEY  イベント配列がネストしている場合のキー（任意）

使い方:
    ECON_CALENDAR_URL=https://example.com/calendar.json python scripts/fetch_calendar.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.calendar import DEFAULT_FIELD_MAP, EconomicCalendar, HttpCalendarProvider  # noqa: E402
from trading.config import Settings  # noqa: E402


def _field_map():
    fm = dict(DEFAULT_FIELD_MAP)
    for key in ("time", "currency", "importance", "title"):
        env = os.environ.get(f"ECON_CALENDAR_FIELD_{key.upper()}")
        if env:
            fm[key] = env
    return fm


def build_calendar(settings: Settings) -> EconomicCalendar:
    url = os.environ.get("ECON_CALENDAR_URL")
    if not url:
        raise SystemExit("ECON_CALENDAR_URL が未設定です。")
    provider = HttpCalendarProvider(
        url=url,
        field_map=_field_map(),
        list_key=os.environ.get("ECON_CALENDAR_LIST_KEY"),
    )
    return EconomicCalendar(provider, settings)


def main() -> int:
    settings = Settings()
    calendar = build_calendar(settings)
    count = calendar.refresh()
    print(f"取得イベント数: {count}")
    print(f"{'time(UTC)':20} {'cur':4} {'impact':7} title")
    print("-" * 60)
    for ev in calendar.events[:50]:
        print(f"{ev.time.strftime('%Y-%m-%d %H:%M'):20} {ev.currency:4} "
              f"{ev.importance:7} {ev.title[:30]}")
    print(f"\nブラックアウト窓: 前{settings.econ_blackout_before_min}分 / "
          f"後{settings.econ_blackout_after_min}分 / 重要度>={settings.econ_importance_min}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
