"""設定。環境変数（.env）から読み込む。

pydantic-settings が無い環境でも動くよう、標準ライブラリへフォールバックする。
本番(live)はデフォルトでは選ばれない（practice が既定）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _split_env(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# OANDA REST エンドポイント（v20）
OANDA_HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}


@dataclass
class Settings:
    """エンジン全体の設定。"""

    # --- ブローカー選択（oanda / paper） ---
    broker: str = field(default_factory=lambda: os.environ.get("BROKER", "oanda"))

    # --- OANDA 接続 ---
    oanda_api_token: str = field(default_factory=lambda: os.environ.get("OANDA_API_TOKEN", ""))
    oanda_account_id: str = field(default_factory=lambda: os.environ.get("OANDA_ACCOUNT_ID", ""))
    # 既定は practice（実弁を誤って触らないための安全装置）
    oanda_env: str = field(default_factory=lambda: os.environ.get("OANDA_ENV", "practice"))

    # --- ペーパートレード（リアル価格＋仮想約定） ---
    paper_account: str = field(default_factory=lambda: os.environ.get("PAPER_ACCOUNT", "default"))
    paper_balance: float = field(default_factory=lambda: _float_env("PAPER_BALANCE", 10000.0))

    # --- 取引対象 ---
    instruments: List[str] = field(
        default_factory=lambda: _split_env("INSTRUMENTS", ["USD_JPY", "EUR_USD"])
    )
    # トリガー足（M15固定運用）と上位足
    trigger_granularity: str = field(
        default_factory=lambda: os.environ.get("TRIGGER_GRANULARITY", "M15")
    )
    htf_granularities: List[str] = field(
        default_factory=lambda: _split_env("HTF_GRANULARITIES", ["H1", "H4", "D"])
    )

    # --- 指標パラメータ ---
    atr_period: int = field(default_factory=lambda: _int_env("ATR_PERIOD", 14))
    adx_period: int = field(default_factory=lambda: _int_env("ADX_PERIOD", 14))
    ema_fast: int = field(default_factory=lambda: _int_env("EMA_FAST", 20))
    ema_mid: int = field(default_factory=lambda: _int_env("EMA_MID", 50))
    ema_slow: int = field(default_factory=lambda: _int_env("EMA_SLOW", 200))

    # --- レジーム判定の閾値 ---
    adx_trend_threshold: float = field(
        default_factory=lambda: _float_env("ADX_TREND_THRESHOLD", 25.0)
    )
    adx_range_threshold: float = field(
        default_factory=lambda: _float_env("ADX_RANGE_THRESHOLD", 20.0)
    )

    # --- エントリー条件 ---
    breakout_lookback: int = field(default_factory=lambda: _int_env("BREAKOUT_LOOKBACK", 20))
    atr_min_pct: float = field(default_factory=lambda: _float_env("ATR_MIN_PCT", 0.30))
    volume_lookback: int = field(default_factory=lambda: _int_env("VOLUME_LOOKBACK", 20))

    # --- リスク管理 ---
    risk_per_trade: float = field(default_factory=lambda: _float_env("RISK_PER_TRADE", 0.5))  # %
    atr_stop_mult: float = field(default_factory=lambda: _float_env("ATR_STOP_MULT", 1.5))
    atr_trail_mult: float = field(default_factory=lambda: _float_env("ATR_TRAIL_MULT", 2.0))
    max_open_positions: int = field(default_factory=lambda: _int_env("MAX_OPEN_POSITIONS", 2))

    # --- ロジック改良（既定は無効＝従来どおり。バックテストで個別にON） ---
    # レンジ回避: トリガー足ADXがこの値未満ならエントリーしない（0=無効）
    entry_adx_min: float = field(default_factory=lambda: _float_env("ENTRY_ADX_MIN", 0.0))
    # 部分利確: +partial_tp_r R に到達したら partial_tp_frac 分を利確し残りは建値ストップ（0=無効）
    partial_tp_r: float = field(default_factory=lambda: _float_env("PARTIAL_TP_R", 0.0))
    partial_tp_frac: float = field(default_factory=lambda: _float_env("PARTIAL_TP_FRAC", 0.5))
    # 強いブレイクのみ: ブレイク足の実体比がこの値以上 & 終値が抜け方向の端寄りのみ許可（0=無効）。
    # 「抜けた直後に逆行」する弱い・ヒゲ主体のダマシ・ブレイクを除外するプライスアクション条件。
    breakout_body_min: float = field(default_factory=lambda: _float_env("BREAKOUT_BODY_MIN", 0.0))
    # リテスト入場: ブレイク足の終値で追いかけず、抜けた水準まで押し戻り（retest）を待ち、
    # そこで支持/抵抗が反転（水準を保って引ける）したら入る。ダマシは retest が成立しないので
    # 構造的に除外され、約定価格も水準近く＝有利。retest_max_bars 本以内に来なければ見送り。
    retest_entry: bool = field(
        default_factory=lambda: os.environ.get("RETEST_ENTRY", "0") in ("1", "true", "True"))
    retest_max_bars: int = field(default_factory=lambda: _int_env("RETEST_MAX_BARS", 8))
    # 構造的な損切り: ATR距離ではなく「レンジの反対側の端」に損切りを置く。
    # 上抜けならレンジ下限の外、下抜けならレンジ上限の外。押し戻りでは狩られず、
    # 「レンジまで丸ごと戻された＝ブレイク失敗」でのみ切る（レンジブレイクの定石）。
    range_stop: bool = field(
        default_factory=lambda: os.environ.get("RANGE_STOP", "0") in ("1", "true", "True"))
    # 本物のレンジ確認: 「上限・下限をそれぞれ複数回タッチし、かつ横ばい（トレンドでない）」
    # ときだけブレイクを有効にする。ただのローリング・チャネル抜けやトレンド中の高値更新を除外。
    range_confirm: bool = field(
        default_factory=lambda: os.environ.get("RANGE_CONFIRM", "0") in ("1", "true", "True"))
    range_min_touches: int = field(default_factory=lambda: _int_env("RANGE_MIN_TOUCHES", 2))

    # --- Phase 5: ダマシ予測ML ---
    fakeout_min_proba: float = field(
        default_factory=lambda: _float_env("FAKEOUT_MIN_PROBA", 0.5)
    )

    # --- 経済指標カレンダー（危険度フィルタ） ---
    econ_blackout_before_min: int = field(
        default_factory=lambda: _int_env("ECON_BLACKOUT_BEFORE_MIN", 30)
    )
    econ_blackout_after_min: int = field(
        default_factory=lambda: _int_env("ECON_BLACKOUT_AFTER_MIN", 15)
    )
    econ_importance_min: str = field(
        default_factory=lambda: os.environ.get("ECON_IMPORTANCE_MIN", "high")
    )

    def __post_init__(self) -> None:
        if self.oanda_env not in OANDA_HOSTS:
            raise ValueError(
                f"OANDA_ENV は {list(OANDA_HOSTS)} のいずれか。指定値: {self.oanda_env!r}"
            )

    @property
    def is_live(self) -> bool:
        return self.oanda_env == "live"

    @property
    def oanda_host(self) -> str:
        return OANDA_HOSTS[self.oanda_env]


def load_settings() -> Settings:
    """環境変数から Settings を生成する。"""
    return Settings()
