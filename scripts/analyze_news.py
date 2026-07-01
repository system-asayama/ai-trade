#!/usr/bin/env python3
"""ニュース/中銀発言を Claude で解析するデモ CLI（Phase 4）。

ANTHROPIC_API_KEY が必要（または `ant auth login` のプロファイル）。

使い方:
    python scripts/analyze_news.py USD_JPY "FRBがタカ派姿勢を強調、追加利上げを示唆"
    echo "ECB総裁がハト派発言" | python scripts/analyze_news.py EUR_USD
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.news import NewsAnalyzer, sentiment_filter  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: analyze_news.py <INSTRUMENT> [text]", file=sys.stderr)
        return 2
    instrument = sys.argv[1]
    text = " ".join(sys.argv[2:]).strip() or sys.stdin.read().strip()
    if not text:
        print("解析するテキストがありません。", file=sys.stderr)
        return 2

    analyzer = NewsAnalyzer()
    s = analyzer.analyze(instrument, text)
    print(f"instrument : {s.instrument}")
    print(f"bias       : {s.bias}")
    print(f"risk_level : {s.risk_level}")
    print(f"confidence : {s.confidence:.2f}")
    print(f"event_type : {s.event_type}")
    print(f"rationale  : {s.rationale}")
    print("--- フィルタ判定（参考） ---")
    for side in ("BUY", "SELL"):
        d = sentiment_filter(side, s)
        print(f"{side}: allow={d.allow} size_factor={d.size_factor} reason={d.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
