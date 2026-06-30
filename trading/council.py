"""AI同士で相場を評価（Phase 5, 複数エージェントの合議）。

複数の Claude「アナリスト」が異なる視点（テクニカル/マクロ/リスク）から
現在のセットアップを評価し、賛否を投票する。多数決で最終判断（エントリー
可否とロットサイズ係数）を出す。

- Anthropic クライアントは注入可能（ネットワーク非依存テスト用）。
- engine に council を渡すと、エントリー前の合議ゲートとして機能する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore

MODEL = "claude-opus-4-8"

VOTE_TRADE = "trade"
VOTE_SKIP = "skip"

_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "vote": {"type": "string", "enum": [VOTE_TRADE, VOTE_SKIP]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["vote", "confidence", "rationale"],
    "additionalProperties": False,
}

# 各メンバーの視点（lens）
DEFAULT_LENSES = {
    "technical": "あなたはテクニカル分析の専門家です。トレンド/モメンタム/ブレイクの質を重視します。",
    "macro": "あなたはマクロ・ファンダメンタルズの専門家です。金利・経済指標・地合いを重視します。",
    "risk": "あなたはリスク管理担当です。下振れリスク・ボラティリティ・損失限定を最優先します。",
}


@dataclass
class MemberVote:
    lens: str
    vote: str
    confidence: float
    rationale: str


@dataclass
class CouncilVerdict:
    allow: bool
    size_factor: float
    reason: str
    votes: List[MemberVote] = field(default_factory=list)

    @property
    def trade_votes(self) -> int:
        return sum(1 for v in self.votes if v.vote == VOTE_TRADE)


class Council:
    def __init__(self, client: Optional[Any] = None, model: str = MODEL,
                 lenses: Optional[Dict[str, str]] = None,
                 min_consensus: float = 0.5) -> None:
        self.model = model
        self.lenses = lenses or DEFAULT_LENSES
        self.min_consensus = min_consensus  # trade票割合の閾値
        if client is not None:
            self._client = client
        elif anthropic is not None:
            self._client = anthropic.Anthropic()
        else:  # pragma: no cover
            self._client = None

    def _ask(self, lens: str, system: str, prompt: str) -> MemberVote:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        data = _extract_json(resp)
        try:
            conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        return MemberVote(lens, str(data.get("vote", VOTE_SKIP)), conf,
                          str(data.get("rationale", "")))

    def evaluate(self, instrument: str, side: str, context: str) -> CouncilVerdict:
        """各メンバーに諮り、多数決で判断する。

        context: 現在のセットアップの要約（指標値・MTF状態・ニュース等）。
        """
        if self._client is None:  # pragma: no cover
            raise RuntimeError("anthropic クライアントが利用できません。")

        prompt = (
            f"通貨ペア: {instrument}\n想定方向: {side}\n\n"
            f"現在のセットアップ:\n{context}\n\n"
            "このトレードを実行(trade)すべきか見送る(skip)か、確信度とともに判断してください。"
        )
        votes = [self._ask(lens, system, prompt) for lens, system in self.lenses.items()]

        n = len(votes)
        trade = sum(1 for v in votes if v.vote == VOTE_TRADE)
        ratio = trade / n if n else 0.0
        allow = ratio >= self.min_consensus
        # 合意度に応じてサイズを調整（賛成多数ほど大きく、最低0.5）
        size_factor = round(max(0.5, ratio), 3) if allow else 0.0
        reason = f"council_{trade}of{n}_trade"
        return CouncilVerdict(allow, size_factor, reason, votes)


def _extract_json(resp: Any) -> Dict[str, Any]:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except (json.JSONDecodeError, AttributeError):
                continue
    raise ValueError("Claude 応答から構造化 JSON を取得できませんでした。")
