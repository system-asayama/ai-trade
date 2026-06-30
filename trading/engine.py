"""ライブ取引エンジン（Phase 2）。

1ティック（M15確定ごと）の処理 `run_once()`:
  1. 口座残高・オープン建玉を取得
  2. 前ティックから消えた建玉＝決済済みを検出し、実現損益をブレーカーへ反映
  3. キルスイッチが入っていれば全決済して終了
  4. 各インストルメントで MTF シグナルを評価
  5. 既存ポジが無く、ブレーカーが許可すれば新規建玉
  6. 既存ポジの SL を ATR トレーリング更新

安全のため live はブレーカー/キルスイッチを通過した場合のみ発注される。
determinism のため当日日付 `today`(YYYY-MM-DD) は呼び出し側が渡す。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import analysis, strategy
from .config import Settings
from .data_feed import DataFeed
from .executor import Executor, OpenTrade, parse_open_trades
from .oanda_client import OandaClient
from .safety import CircuitBreaker

logger = logging.getLogger("trading.engine")


@dataclass
class TickResult:
    balance: float = 0.0
    entries: List[str] = field(default_factory=list)
    trails_updated: int = 0
    closes_registered: int = 0
    killed: bool = False
    blocked: Dict[str, str] = field(default_factory=dict)  # instrument -> 理由


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        client: OandaClient,
        feed: Optional[DataFeed] = None,
        executor: Optional[Executor] = None,
        breaker: Optional[CircuitBreaker] = None,
        store=None,
        news_provider=None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.feed = feed or DataFeed(settings, client)
        self.executor = executor or Executor(settings, client)
        self.breaker = breaker or CircuitBreaker(settings)
        self.store = store  # TradeStore（任意）。None なら永続化しない
        # ニュースセンチメント提供元（任意）。latest(instrument)->NewsSentiment|None
        self.news_provider = news_provider
        self._known_ids: Set[str] = set()

    # -- 1ティック -----------------------------------------------------------
    def run_once(self, today: str) -> TickResult:
        result = TickResult()

        summary = self.client.get_account_summary()
        result.balance = float(summary.get("balance") or summary.get("NAV") or 0.0)

        open_trades = parse_open_trades(self.client.get_open_trades())
        open_ids = {t.trade_id for t in open_trades}

        # 決済済みトレードの損益をブレーカーへ反映
        for closed_id in self._known_ids - open_ids:
            try:
                trade = self.client.get_trade(closed_id)
                pnl = float(trade.get("realizedPL") or 0.0)
                self.breaker.register_close(pnl, today)
                result.closes_registered += 1
                if self.store is not None:
                    exit_price = trade.get("averageClosePrice") or trade.get("price")
                    self.store.record_close(
                        oanda_trade_id=closed_id,
                        exit_time=trade.get("closeTime") or today,
                        exit_price=float(exit_price) if exit_price else None,
                        pnl=pnl,
                        exit_reason="closed",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("決済損益の取得失敗 trade=%s: %s", closed_id, exc)
        self._known_ids = open_ids

        # キルスイッチ
        if self.breaker.state.killed:
            self.executor.close_all(open_trades)
            result.killed = True
            logger.warning("キルスイッチ作動: 全建玉を決済")
            return result

        instruments_with_pos = {t.instrument for t in open_trades}
        atr_by_instrument: Dict[str, float] = {}
        entered = 0

        for inst in self.settings.instruments:
            frames = self.feed.fetch_multi_timeframe(inst)
            trig = analysis.add_indicators(frames[self.settings.trigger_granularity],
                                           self.settings)
            if not trig.empty:
                atr_by_instrument[inst] = float(trig["atr"].iloc[-1] or 0.0)

            htf = {
                g: analysis.add_indicators(frames[g], self.settings)
                for g in self.settings.htf_granularities
                if g in frames
            }
            mtf = analysis.evaluate_mtf(htf)
            signal = strategy.evaluate(trig, mtf, self.settings)

            if inst in instruments_with_pos:
                continue  # 既存ポジがあるインストルメントは増し玉しない

            if not signal.is_entry:
                continue

            ok, reason = self.breaker.can_open(len(open_trades) + entered, today)
            if not ok:
                result.blocked[inst] = reason
                continue

            # ニュース/中銀発言フィルタ（任意）
            size_factor = 1.0
            decision = self._news_decision(inst, signal.side)
            if decision is not None:
                if not decision.allow:
                    result.blocked[inst] = decision.reason
                    continue
                size_factor = decision.size_factor

            client_id = self._client_id(inst, trig)
            rate = self._quote_to_account_rate(inst, summary)
            order = self.executor.open_position(
                signal, inst, result.balance, client_id=client_id,
                quote_to_account_rate=rate, size_factor=size_factor,
            )
            if order is not None:
                entered += 1
                result.entries.append(f"{inst}:{order.side}:{order.units}")
                if self.store is not None:
                    self.store.record_open(
                        instrument=inst,
                        side=order.side,
                        units=order.units,
                        entry_time=trig.index[-1],
                        entry_price=order.entry_price,
                        stop_loss=order.stop_loss,
                        environment=self.settings.oanda_env,
                        oanda_trade_id=order.oanda_trade_id,
                        client_id=client_id,
                        entry_features=signal.reason,
                    )

        # トレーリング
        result.trails_updated = self.executor.trail_stops(open_trades, atr_by_instrument)
        return result

    # -- 常駐ループ ----------------------------------------------------------
    def run_forever(self, poll_seconds: int = 60, max_ticks: Optional[int] = None) -> None:
        """簡易常駐ループ。max_ticks 指定時はその回数で停止（テスト/検証用）。"""
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            try:
                res = self.run_once(today)
                logger.info("tick: entries=%s trails=%d killed=%s",
                            res.entries, res.trails_updated, res.killed)
            except Exception as exc:  # noqa: BLE001 ループは落とさない
                logger.exception("run_once 失敗: %s", exc)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            time.sleep(poll_seconds)

    # -- ヘルパ --------------------------------------------------------------
    def _client_id(self, instrument: str, trig) -> str:
        """同一バーの重複発注を防ぐための決定論的 ID。"""
        ts = int(trig.index[-1].value) if len(trig) else 0
        return f"{instrument}-{self.settings.trigger_granularity}-{ts}"

    def _news_decision(self, instrument: str, side: str):
        """ニュースセンチメントからエントリー可否/サイズ係数を判定する。

        news_provider 未設定なら None（フィルタ無効）。
        """
        if self.news_provider is None:
            return None
        from .news import sentiment_filter

        sentiment = self.news_provider.latest(instrument)
        return sentiment_filter(side, sentiment)

    def _quote_to_account_rate(self, instrument: str, summary: dict) -> float:
        """quote 通貨→口座通貨の換算レート。口座通貨==quote のとき 1.0。

        それ以外は Phase 2 では 1.0 近似（精緻な多通貨換算は今後の課題）。
        """
        account_ccy = (summary.get("currency") or "").upper()
        quote_ccy = instrument.split("_")[-1].upper() if "_" in instrument else ""
        if account_ccy and quote_ccy and account_ccy == quote_ccy:
            return 1.0
        return 1.0
