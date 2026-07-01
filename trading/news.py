"""ニュース・中央銀行発言の解析（Phase 4, Claude API）。

Claude（claude-opus-4-8）でニュース見出し/本文・中銀声明を解析し、
通貨ペアに対する方向バイアス・リスク度・確信度を構造化出力で得る。
その結果をエントリー可否とロットサイズの補助フィルタに使う。

- ネットワーク非依存のテストのため、Anthropic クライアントは注入可能。
- anthropic 未インストールでもモジュールの import は可能（実呼び出し時に検証）。
- 構造化出力は output_config.format（json_schema）で型を保証する。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

try:  # 実呼び出し時のみ必要
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore

logger = logging.getLogger("trading.news")

MODEL = "claude-opus-4-8"

# 方向バイアス（対象インストルメントが上がるか下がるか）
BIAS_BULLISH = "bullish"
BIAS_BEARISH = "bearish"
BIAS_NEUTRAL = "neutral"

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# Claude に強制する出力スキーマ（構造化出力）
_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "bias": {"type": "string", "enum": [BIAS_BULLISH, BIAS_BEARISH, BIAS_NEUTRAL]},
        "risk_level": {"type": "string", "enum": [RISK_LOW, RISK_MEDIUM, RISK_HIGH]},
        "confidence": {"type": "number"},
        "event_type": {
            "type": "string",
            "enum": ["central_bank", "economic_data", "geopolitical", "other"],
        },
        "rationale": {"type": "string"},
    },
    "required": ["bias", "risk_level", "confidence", "event_type", "rationale"],
    "additionalProperties": False,
}

_SYSTEM = (
    "あなたはFX市場のシニアアナリストです。与えられたニュースや中央銀行の発言が、"
    "指定された通貨ペアの価格に与える短期的な影響を評価します。"
    "bias は対象ペアが『上昇(bullish)/下落(bearish)/中立(neutral)』のいずれに向かいやすいか。"
    "risk_level はこの材料による短期ボラティリティ/急変リスク（high なら新規建ては避けるべき）。"
    "confidence は 0.0〜1.0 の確信度。過度な断定は避け、不確実なら中立・低確信にする。"
    "投資助言ではなく、客観的な市場影響評価のみを行うこと。"
)


@dataclass
class NewsSentiment:
    instrument: str
    bias: str
    risk_level: str
    confidence: float
    event_type: str
    rationale: str
    headline: str = ""

    @property
    def is_high_risk(self) -> bool:
        return self.risk_level == RISK_HIGH

    @classmethod
    def from_dict(cls, instrument: str, data: Dict[str, Any], headline: str = "") -> "NewsSentiment":
        conf = data.get("confidence", 0.0)
        try:
            conf = max(0.0, min(1.0, float(conf)))  # 0..1 にクランプ
        except (TypeError, ValueError):
            conf = 0.0
        return cls(
            instrument=instrument,
            bias=str(data.get("bias", BIAS_NEUTRAL)),
            risk_level=str(data.get("risk_level", RISK_MEDIUM)),
            confidence=conf,
            event_type=str(data.get("event_type", "other")),
            rationale=str(data.get("rationale", "")),
            headline=headline,
        )


class NewsAnalyzer:
    """Claude を用いてニュース/中銀発言を解析する。"""

    def __init__(self, client: Optional[Any] = None, model: str = MODEL) -> None:
        self.model = model
        if client is not None:
            self._client = client
        elif anthropic is not None:
            self._client = anthropic.Anthropic()  # 認証は環境から解決
        else:  # pragma: no cover
            self._client = None

    def analyze(self, instrument: str, text: str) -> NewsSentiment:
        """1件のニュース/発言を解析して NewsSentiment を返す。"""
        if self._client is None:  # pragma: no cover
            raise RuntimeError("anthropic クライアントが利用できません。")

        prompt = (
            f"通貨ペア: {instrument}\n"
            f"以下のニュース/発言が {instrument} に与える短期的影響を評価してください。\n\n"
            f"---\n{text}\n---"
        )
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        data = _extract_json(resp)
        return NewsSentiment.from_dict(instrument, data, headline=text[:120])


def _extract_json(resp: Any) -> Dict[str, Any]:
    """messages.create のレスポンスから JSON 本文を取り出す。"""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except (json.JSONDecodeError, AttributeError):
                continue
    raise ValueError("Claude 応答から構造化 JSON を取得できませんでした。")


class SentimentStore:
    """インストルメントごとに最新のニュースセンチメントを保持する簡易ストア。

    実運用ではニュースフィードから受信した見出しを analyzer で解析し、
    update() で更新する。engine は latest() で参照する。
    """

    def __init__(self, analyzer: Optional[NewsAnalyzer] = None) -> None:
        self.analyzer = analyzer
        self._latest: Dict[str, NewsSentiment] = {}

    def update_from_text(self, instrument: str, text: str) -> NewsSentiment:
        if self.analyzer is None:  # pragma: no cover
            raise RuntimeError("analyzer が設定されていません。")
        sentiment = self.analyzer.analyze(instrument, text)
        self._latest[instrument] = sentiment
        return sentiment

    def set(self, sentiment: NewsSentiment) -> None:
        self._latest[sentiment.instrument] = sentiment

    def latest(self, instrument: str) -> Optional[NewsSentiment]:
        return self._latest.get(instrument)


@dataclass
class NewsDecision:
    allow: bool
    size_factor: float  # 1.0=通常, <1.0=縮小, 0.0=見送り
    reason: str
    sentiment: Optional[NewsSentiment] = None


def sentiment_filter(
    side: str,
    sentiment: Optional[NewsSentiment],
    contradiction_confidence: float = 0.6,
) -> NewsDecision:
    """シグナル方向とニュースセンチメントからエントリー可否/サイズ係数を決める。

    - 高リスク材料（指標・中銀直後の急変リスク）→ 見送り
    - 高確信でシグナルと逆方向 → 見送り
    - 同方向（追い風）→ 通常サイズ
    - それ以外（中立/低確信）→ 確信度に応じて軽く縮小
    """
    if sentiment is None:
        return NewsDecision(True, 1.0, "no_news")

    if sentiment.is_high_risk:
        return NewsDecision(False, 0.0, "news_high_risk", sentiment)

    # シグナルが示す方向（BUY=bullish期待 / SELL=bearish期待）
    signal_bias = BIAS_BULLISH if side == "BUY" else BIAS_BEARISH
    opposite = BIAS_BEARISH if signal_bias == BIAS_BULLISH else BIAS_BULLISH

    if sentiment.bias == opposite and sentiment.confidence >= contradiction_confidence:
        return NewsDecision(False, 0.0, "news_contradicts", sentiment)

    if sentiment.bias == signal_bias:
        return NewsDecision(True, 1.0, "news_supports", sentiment)

    # 中立 or 弱い逆風: 確信度に応じてサイズを 0.5〜1.0 に縮小
    size_factor = max(0.5, 1.0 - sentiment.confidence * 0.5)
    return NewsDecision(True, round(size_factor, 3), "news_neutral", sentiment)
