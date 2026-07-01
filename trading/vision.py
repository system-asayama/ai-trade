"""チャート画像認識（Phase 5, Claude vision）。

ローソク足チャートの画像を Claude（claude-opus-4-8）に渡し、トレンド・
チャートパターン・ダマシリスクを構造化出力で読み取る。

- Anthropic クライアントは注入可能（ネットワーク非依存テスト用）。
- 画像生成は matplotlib があれば利用（render_chart）。無くても解析は
  既存の PNG/画像バイト列に対して動作する。
"""
from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore

MODEL = "claude-opus-4-8"

_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "trend": {"type": "string", "enum": ["up", "down", "range"]},
        "pattern": {"type": "string"},
        "fakeout_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["trend", "pattern", "fakeout_risk", "confidence", "rationale"],
    "additionalProperties": False,
}

_SYSTEM = (
    "あなたはテクニカルアナリストです。提示されたローソク足チャート画像を読み、"
    "trend（上昇/下降/レンジ）、代表的なチャートパターン（pattern）、直近ブレイクの"
    "ダマシリスク（fakeout_risk）、確信度（confidence 0.0〜1.0）を評価します。"
    "客観的な視覚的評価のみを行い、投資助言はしません。"
)


@dataclass
class ChartRead:
    instrument: str
    trend: str
    pattern: str
    fakeout_risk: str
    confidence: float
    rationale: str

    @classmethod
    def from_dict(cls, instrument: str, data: Dict[str, Any]) -> "ChartRead":
        try:
            conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        return cls(
            instrument=instrument,
            trend=str(data.get("trend", "range")),
            pattern=str(data.get("pattern", "")),
            fakeout_risk=str(data.get("fakeout_risk", "medium")),
            confidence=conf,
            rationale=str(data.get("rationale", "")),
        )


class ChartAnalyzer:
    def __init__(self, client: Optional[Any] = None, model: str = MODEL) -> None:
        self.model = model
        if client is not None:
            self._client = client
        elif anthropic is not None:
            self._client = anthropic.Anthropic()
        else:  # pragma: no cover
            self._client = None

    def analyze(self, instrument: str, image_bytes: bytes,
                media_type: str = "image/png") -> ChartRead:
        if self._client is None:  # pragma: no cover
            raise RuntimeError("anthropic クライアントが利用できません。")
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text",
                     "text": f"{instrument} のチャートです。視覚的に評価してください。"},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        return ChartRead.from_dict(instrument, _extract_json(resp))


def _extract_json(resp: Any) -> Dict[str, Any]:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except (json.JSONDecodeError, AttributeError):
                continue
    raise ValueError("Claude 応答から構造化 JSON を取得できませんでした。")


def render_chart(df, title: str = "") -> Optional[bytes]:
    """OHLCV DataFrame からローソク足風チャートの PNG を生成する。

    matplotlib が無い環境では None を返す（解析自体は既存画像で可能）。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        return None

    fig, ax = plt.subplots(figsize=(8, 4))
    idx = range(len(df))
    for i, (_, row) in zip(idx, df.iterrows()):
        color = "#2e7d32" if row["close"] >= row["open"] else "#c62828"
        ax.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.6)
        ax.plot([i, i], [row["open"], row["close"]], color=color, linewidth=3)
    ax.set_title(title)
    ax.set_xticks([])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=90, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
