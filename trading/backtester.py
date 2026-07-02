"""イベントドリブンのバックテスト基盤（Phase 1）。

トリガー足(M15)の各バーを時系列に進め、上位足は同一データから
リサンプルして「その時点までに確定した足」の状態だけを参照する
（ルックアヘッド・バイアスを避ける）。

損益はインストルメント非依存の **R倍数**（初期リスク幅で正規化）を主指標とし、
価格ポイントの損益も併記する。実運用のロット/通貨換算は Phase 2 で扱う。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import analysis, strategy
from .analysis import MTFView, TREND_DOWN, TREND_RANGE, TREND_UP
from .config import Settings
from .data_feed import resample_ohlcv
from .strategy import SIGNAL_BUY, SIGNAL_SELL


@dataclass
class BacktestTrade:
    instrument: str
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    stop: float
    initial_risk: float = 0.0  # エントリー時の初期リスク幅（R正規化の基準）
    exit_time: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    r_multiple: float = 0.0
    pnl_points: float = 0.0
    entry_reason: Dict[str, object] = field(default_factory=dict)  # ML特徴量の素
    # 部分利確の管理
    banked_r: float = 0.0        # 部分利確で確定したR
    remaining_frac: float = 1.0  # 残りポジション比率
    partial_taken: bool = False

    @property
    def is_open(self) -> bool:
        return self.exit_time is None


@dataclass
class BacktestResult:
    instrument: str
    trades: List[BacktestTrade] = field(default_factory=list)
    # 各フィルタ段階を何本のバーが通過したかの集計（「なぜ0取引か」を可視化する）
    diagnostics: Dict[str, int] = field(default_factory=dict)

    @property
    def closed(self) -> List[BacktestTrade]:
        return [t for t in self.trades if not t.is_open]

    @property
    def num_trades(self) -> int:
        return len(self.closed)

    @property
    def win_rate(self) -> float:
        if not self.closed:
            return 0.0
        wins = sum(1 for t in self.closed if t.r_multiple > 0)
        return wins / len(self.closed)

    @property
    def total_r(self) -> float:
        return sum(t.r_multiple for t in self.closed)

    @property
    def expectancy_r(self) -> float:
        return self.total_r / len(self.closed) if self.closed else 0.0

    @property
    def max_drawdown_r(self) -> float:
        """Rベースのエクイティ曲線の最大ドローダウン。"""
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.closed:
            equity += t.r_multiple
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return max_dd

    def summary(self) -> Dict[str, object]:
        return {
            "instrument": self.instrument,
            "num_trades": self.num_trades,
            "win_rate": round(self.win_rate, 4),
            "total_r": round(self.total_r, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "max_drawdown_r": round(self.max_drawdown_r, 4),
        }

    def analytics(self) -> Dict[str, object]:
        """成績の内訳（勝ち負けの偏り・年別・決済理由別）を返す。

        「なぜこの成績なのか」を数字で示すための分析。トレンド追随系では
        少数の大勝ちが多数の小負けを支える形になりやすく、payoff（勝ち平均÷
        負け平均）と profit_factor（総利益÷総損失）で健全性が見える。
        """
        closed = self.closed
        wins = [t.r_multiple for t in closed if t.r_multiple > 0]
        losses = [t.r_multiple for t in closed if t.r_multiple <= 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)  # 正の値
        avg_win = gross_win / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0

        # 決済理由別
        by_reason: Dict[str, Dict[str, float]] = {}
        for t in closed:
            r = by_reason.setdefault(t.exit_reason or "?", {"count": 0, "total_r": 0.0})
            r["count"] += 1
            r["total_r"] += t.r_multiple

        # 年別（どの時期に稼ぎ/損したか＝レジーム依存の可視化）
        by_year: Dict[str, Dict[str, float]] = {}
        for t in closed:
            yr = str(t.entry_time.year) if t.entry_time is not None else "?"
            y = by_year.setdefault(yr, {"count": 0, "total_r": 0.0})
            y["count"] += 1
            y["total_r"] += t.r_multiple

        return {
            "avg_win_r": round(avg_win, 3),
            "avg_loss_r": round(-avg_loss, 3),  # 表示は負の値で
            "payoff": round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else 0.0,
            "largest_win_r": round(max(wins), 2) if wins else 0.0,
            "largest_loss_r": round(min(losses), 2) if losses else 0.0,
            "num_wins": len(wins),
            "num_losses": len(losses),
            "by_reason": {k: {"count": int(v["count"]), "total_r": round(v["total_r"], 2)}
                          for k, v in by_reason.items()},
            "by_year": {k: {"count": int(v["count"]), "total_r": round(v["total_r"], 2)}
                        for k, v in sorted(by_year.items())},
        }


class Backtester:
    """単一インストルメント・単一ポジションのバックテスター。"""

    def __init__(self, settings: Settings, spread_pips: float = 0.0,
                 slippage_pips: float = 0.0) -> None:
        self.settings = settings
        # 取引コスト（pips）。スプレッドは往復で不利に、滑りは片側ごとに不利に働く
        self.spread_pips = float(spread_pips)
        self.slippage_pips = float(slippage_pips)
        self._pip = 0.01  # run() で通貨ペアに応じて設定

    def _cost(self) -> float:
        """片側あたりの不利な価格ずれ（スプレッド半分＋滑り）を価格単位で返す。"""
        return self._pip * (self.spread_pips / 2.0 + self.slippage_pips)

    def _htf_states_at(
        self, htf_indicators: Dict[str, pd.DataFrame], when: pd.Timestamp
    ) -> MTFView:
        """各上位足について when までに確定した最新バーのトレンド状態を取る。"""
        states: Dict[str, str] = {}
        for gran, df in htf_indicators.items():
            # when 以下の最後の行を取得
            pos = df.index.searchsorted(when, side="right") - 1
            if pos < 0:
                states[gran] = TREND_RANGE
            else:
                value = df["trend_state"].iloc[pos]
                states[gran] = value if isinstance(value, str) else TREND_RANGE
        values = set(states.values())
        from .analysis import TREND_DOWN, TREND_UP

        if values == {TREND_UP}:
            aligned: Optional[str] = TREND_UP
        elif values == {TREND_DOWN}:
            aligned = TREND_DOWN
        else:
            aligned = None
        return MTFView(states=states, aligned=aligned)

    def _precompute_htf(
        self, htf_indicators: Dict[str, pd.DataFrame], tindex: pd.DatetimeIndex
    ):
        """各トリガー足時点の上位足トレンド状態を一括計算する（ベクトル化）。

        毎バー searchsorted+iloc していた _htf_states_at をループ外で1回に。
        戻り値: (gran別 状態配列 dict, 方向一致配列[UP/DOWN/None])。
        """
        n = len(tindex)
        gran_states: Dict[str, np.ndarray] = {}
        for gran, df in htf_indicators.items():
            arr = np.full(n, TREND_RANGE, dtype=object)
            if len(df):
                states = df["trend_state"].to_numpy()
                # 各トリガー時刻について「その時刻までに確定した最新の上位足」位置
                pos = df.index.searchsorted(tindex, side="right") - 1
                valid = pos >= 0
                arr[valid] = states[pos[valid]]
            gran_states[gran] = arr

        grans = list(gran_states.keys())
        if grans:
            stacked = np.stack([gran_states[g] for g in grans], axis=1)
            all_up = np.all(stacked == TREND_UP, axis=1)
            all_down = np.all(stacked == TREND_DOWN, axis=1)
        else:
            all_up = np.zeros(n, dtype=bool)
            all_down = np.zeros(n, dtype=bool)
        aligned = np.where(all_up, TREND_UP, np.where(all_down, TREND_DOWN, None))
        return gran_states, aligned

    def _train_fakeout(self, result: BacktestResult):
        """count_from より前に確定した取引だけで ダマシ予測モデルを学習する。

        学習に使うのは評価期間より前の取引のみ（＝先読みなし）。サンプルが
        少なすぎるときは学習せず None（フィルタ無効）。
        """
        from .ml import FakeoutModel, features_from_reason
        X, y = [], []
        for t in result.trades:
            if t.is_open or not t.entry_reason:
                continue
            X.append(features_from_reason(t.entry_reason))
            y.append(1.0 if t.r_multiple > 0 else 0.0)
        if len(y) < 40 or len(set(y)) < 2:  # データ不足 or 片側だけ
            return None
        return FakeoutModel().fit(np.asarray(X), np.asarray(y))

    def run(self, instrument: str, trigger_df: pd.DataFrame,
            count_from: Optional[pd.Timestamp] = None,
            fakeout_ml: bool = False) -> BacktestResult:
        """トリガー足(生OHLCV)を与えてバックテストを実行する。

        上位足は trigger_df から settings.htf_granularities へリサンプルする。

        count_from を指定すると、シミュレーション自体は全期間で行う（＝指標を
        十分に暖機する）が、集計・取引記録は count_from 以降のエントリーだけに
        限定する。これにより「短い期間は長い期間の一部分」という関係が必ず成り立ち、
        期間の切り出し方で成績が食い違うのを防ぐ（暖機不足による幻の取引を排除）。
        """
        settings = self.settings
        result = BacktestResult(instrument=instrument)
        self._pip = 0.01 if instrument.endswith("_JPY") else 0.0001

        trigger = analysis.add_indicators(trigger_df, settings)

        htf_indicators: Dict[str, pd.DataFrame] = {}
        for gran in settings.htf_granularities:
            resampled = resample_ohlcv(trigger_df, gran)
            htf_indicators[gran] = analysis.add_indicators(resampled, settings)

        # 指標が安定するまでのウォームアップ
        warmup = max(settings.ema_slow, settings.breakout_lookback + 1)
        position: Optional[BacktestTrade] = None
        # リテスト入場の保留注文: (side, level, atr, 残りバー数) or None
        pending = None

        # 「なぜエントリーしなかったか」を段階別に集計する
        diag = {
            "bars": 0,             # 評価したバー数
            "mtf_aligned": 0,      # 上位足の方向が一致していたバー
            "breakout": 0,         # 方向一致 かつ その向きにブレイクしたバー
            "atr_pass": 0,         # さらにATR（勢い）条件を満たしたバー
            "weak_filtered": 0,    # 弱いブレイク（強いブレイクのみ）で見送ったバー
            "range_filtered": 0,   # レンジ回避フィルタで見送ったバー
            "ml_filtered": 0,      # ダマシAIで見送ったバー
            "entries": 0,          # 最終的にエントリー条件を満たした信号
        }
        # ダマシAI用モデル（count_from を跨いだ時点で学習して以降に適用）
        model = None
        ml_trained = False
        _PASSED_BREAKOUT = {"weakbreak", "atr", "volume", "regime", "fakeout", "entry"}
        _PASSED_ATR = {"volume", "regime", "fakeout", "entry"}

        # strategy.evaluate は直近の一定本数しか参照しないため、毎バー全履歴を
        # スライスせず「直近 window 本」だけ渡す（O(n^2)→O(n) に高速化）。
        window = max(settings.breakout_lookback + 1, settings.volume_lookback + 1, 150)

        # --- 高速化: 上位足の方向一致をループ外で一括計算 -------------------
        # 上位足がそろわない限り strategy.evaluate は必ず NONE を返す。
        # 一致は全体の数%しかないため、そろったバーだけ evaluate を呼ぶ。
        tindex = trigger.index
        gran_states, aligned_arr = self._precompute_htf(htf_indicators, tindex)

        # 毎バーの iloc（pandas 行アクセス）を避けるため列を numpy 配列に展開
        highs = trigger["high"].to_numpy(dtype=float)
        lows = trigger["low"].to_numpy(dtype=float)
        closes = trigger["close"].to_numpy(dtype=float)
        atrs = (trigger["atr"].to_numpy(dtype=float)
                if "atr" in trigger.columns else np.zeros(len(trigger)))

        for i in range(warmup, len(trigger)):
            when = tindex[i]
            close_i = closes[i]
            counting = count_from is None or when >= count_from

            # 評価期間に入る瞬間に、それ以前の取引だけでダマシAIを学習する
            if fakeout_ml and not ml_trained and counting and count_from is not None:
                model = self._train_fakeout(result)
                ml_trained = True

            # --- 既存ポジションの管理（当該バーで先に決済判定） ---
            if position is not None:
                position = self._manage_position(
                    position, highs[i], lows[i], close_i, atrs[i], when, result)

            # --- リテスト待ち（保留注文）の処理：毎バー・ポジション無しのとき ---
            # 抜けた水準まで押し戻り、そこを保って引ければ入場（＝ダマシは成立しない）。
            # pending = (side, レンジ上限, レンジ下限, atr, 残りバー数)
            if position is None and pending is not None:
                pside, rtop, rbot, patr, pleft = pending
                pending = None  # このバーで確定 or 消化。残れば下で作り直す
                if pside == SIGNAL_BUY and lows[i] <= rtop:
                    if close_i >= rtop:  # 抜けた上限まで戻り、保って引けた＝成立
                        stop = self._entry_stop(SIGNAL_BUY, close_i, patr, rtop, rbot)
                        position = self._make_trade(
                            instrument, SIGNAL_BUY, close_i, stop, when, {"stage": "retest"})
                    # 割って引けた＝ダマシ → 何もしない（見送り）
                elif pside == SIGNAL_SELL and highs[i] >= rbot:
                    if close_i <= rbot:
                        stop = self._entry_stop(SIGNAL_SELL, close_i, patr, rtop, rbot)
                        position = self._make_trade(
                            instrument, SIGNAL_SELL, close_i, stop, when, {"stage": "retest"})
                else:
                    pleft -= 1  # まだ水準に触れていない → 期限まで待つ
                    if pleft > 0:
                        pending = (pside, rtop, rbot, patr, pleft)

            if counting:
                diag["bars"] += 1

            # --- 上位足がそろっていなければ評価不要（evaluate は NONE 確定） ---
            aligned = aligned_arr[i]
            if aligned is None:
                continue
            if counting:
                diag["mtf_aligned"] += 1

            # そろったバーだけ直近 window 本をスライスして本評価
            slice_df = trigger.iloc[max(0, i - window + 1): i + 1]
            mtf = MTFView(states={g: gran_states[g][i] for g in gran_states},
                          aligned=aligned)
            signal = strategy.evaluate(slice_df, mtf, settings, fakeout_model=model)

            if counting:
                stage = signal.reason.get("stage")
                if stage in _PASSED_BREAKOUT:
                    diag["breakout"] += 1
                if stage in _PASSED_ATR:
                    diag["atr_pass"] += 1
                if stage == "weakbreak":
                    diag["weak_filtered"] += 1
                elif stage == "regime":
                    diag["range_filtered"] += 1
                elif stage == "fakeout":
                    diag["ml_filtered"] += 1
                elif stage == "entry":
                    diag["entries"] += 1

            if position is not None:
                # 反対シグナルが出たらクローズ
                if signal.is_entry and signal.side != position.side:
                    self._close(position, when, close_i, "opposite_signal", result)
                    position = None

            if position is None and signal.is_entry:
                # ブレイクしたレンジの上限/下限（＝損切りの基準になる構造）
                lb = settings.breakout_lookback
                lo = max(0, i - lb)
                rtop = float(highs[lo:i].max()) if i > lo else close_i
                rbot = float(lows[lo:i].min()) if i > lo else close_i
                if settings.retest_entry:
                    # 追いかけず、抜けた水準への押し戻りを待つ保留にする
                    pending = (signal.side, rtop, rbot, atrs[i], settings.retest_max_bars)
                else:
                    stop = self._entry_stop(signal.side, signal.price, atrs[i], rtop, rbot)
                    position = self._make_trade(instrument, signal.side, signal.price,
                                                stop, when, signal.reason)

        # 最終バーで残ポジは終値クローズ
        if position is not None:
            self._close(position, tindex[-1], closes[-1], "end_of_data", result)

        # 集計期間より前に建てた取引（＝暖機用）は成績に含めない
        if count_from is not None:
            result.trades = [t for t in result.trades if t.entry_time >= count_from]

        result.diagnostics = diag
        return result

    # -- ポジション操作 ------------------------------------------------------
    def _make_trade(self, instrument, side, ref_price, stop, when, reason) -> BacktestTrade:
        """約定価格にコスト（スプレッド/滑り）を乗せてトレードを生成する。"""
        cost = self._cost()
        entry = ref_price + cost if side == SIGNAL_BUY else ref_price - cost
        return BacktestTrade(
            instrument=instrument,
            side=side,
            entry_time=when,
            entry_price=entry,
            stop=stop,
            initial_risk=abs(entry - stop),
            entry_reason=dict(reason) if reason else {},
        )

    def _entry_stop(self, side, ref_price, atr, range_top, range_bottom) -> float:
        """初期ストップ価格を返す。

        range_stop=True なら「レンジの反対側の端」（上抜けは下限、下抜けは上限）。
        それ以外は従来どおり建値から ATR×倍率 の距離。
        """
        s = self.settings
        if s.range_stop:
            return float(range_bottom if side == SIGNAL_BUY else range_top)
        dist = s.atr_stop_mult * float(atr or 0.0)
        return ref_price - dist if side == SIGNAL_BUY else ref_price + dist

    def _open(self, instrument: str, signal, bar_atr, when) -> BacktestTrade:
        atr_value = signal.atr or float(bar_atr or 0.0)
        dist = self.settings.atr_stop_mult * atr_value
        stop = signal.price - dist if signal.side == SIGNAL_BUY else signal.price + dist
        return self._make_trade(instrument, signal.side, signal.price, stop, when,
                                signal.reason)

    def _try_partial_tp(self, pos: BacktestTrade, high, low) -> None:
        """+partial_tp_r R に到達したら一部利確し、残りは建値ストップにする。"""
        s = self.settings
        if s.partial_tp_r <= 0 or pos.partial_taken or pos.initial_risk <= 0:
            return
        target_dist = s.partial_tp_r * pos.initial_risk
        cost = self._cost()
        if pos.side == SIGNAL_BUY:
            target = pos.entry_price + target_dist
            if high >= target:
                leg_r = (target - cost - pos.entry_price) / pos.initial_risk
                pos.banked_r += s.partial_tp_frac * leg_r
                pos.remaining_frac = 1.0 - s.partial_tp_frac
                pos.partial_taken = True
                pos.stop = max(pos.stop, pos.entry_price)  # 建値へ
        else:  # SELL
            target = pos.entry_price - target_dist
            if low <= target:
                leg_r = (pos.entry_price - (target + cost)) / pos.initial_risk
                pos.banked_r += s.partial_tp_frac * leg_r
                pos.remaining_frac = 1.0 - s.partial_tp_frac
                pos.partial_taken = True
                pos.stop = min(pos.stop, pos.entry_price)

    def _manage_position(self, pos: BacktestTrade, high, low, close, atr_value,
                         when, result) -> Optional[BacktestTrade]:
        """ストップ判定 → 部分利確 → トレーリング更新。決済したら None を返す。"""
        if pos.side == SIGNAL_BUY:
            if low <= pos.stop:  # ストップ約定（保守的に先に判定）
                # 建値以上に引き上がったストップで出た＝利益方向の手仕舞い(trail)、
                # 建値未満＝本来の損切り(stop) として区別する（分析用）。
                reason = "trail" if pos.stop > pos.entry_price else "stop"
                self._close(pos, when, pos.stop, reason, result)
                return None
            self._try_partial_tp(pos, high, low)
            # トレーリング（建値方向にのみ引き上げ）
            new_stop = close - self.settings.atr_trail_mult * atr_value
            pos.stop = max(pos.stop, float(new_stop))
        else:  # SELL
            if high >= pos.stop:
                reason = "trail" if pos.stop < pos.entry_price else "stop"
                self._close(pos, when, pos.stop, reason, result)
                return None
            self._try_partial_tp(pos, high, low)
            new_stop = close + self.settings.atr_trail_mult * atr_value
            pos.stop = min(pos.stop, float(new_stop))
        return pos

    def _close(self, pos: BacktestTrade, when, price, reason, result: BacktestResult) -> None:
        cost = self._cost()
        # 決済もスプレッド/滑りの分だけ不利側で約定する
        if pos.side == SIGNAL_BUY:
            exit_price = price - cost
            pos.pnl_points = exit_price - pos.entry_price
        else:
            exit_price = price + cost
            pos.pnl_points = pos.entry_price - exit_price
        pos.exit_time = when
        pos.exit_price = exit_price
        pos.exit_reason = reason
        # R倍数はエントリー時の初期リスク幅で正規化（トレーリング後のストップではない）。
        # 部分利確済みなら「確定分 + 残り比率×残りレッグのR」で合成する。
        risk = pos.initial_risk
        leg_r = pos.pnl_points / risk if risk > 0 else 0.0
        pos.r_multiple = pos.banked_r + pos.remaining_frac * leg_r
        result.trades.append(pos)


def diagnose(summary: Dict[str, object], analytics: Dict[str, object]) -> List[Dict[str, str]]:
    """成績の内訳を「なぜこの結果になったのか」の平易な日本語に翻訳する。

    数字（勝率・ペイオフ・PF・決済理由・年別）から典型的な不調パターンを
    判定し、初心者にも分かる説明と改善の方向を返す。
    戻り値: [{"level": "bad|warn|good|info", "text": ...}, ...]
    """
    out: List[Dict[str, str]] = []

    def add(level: str, text: str) -> None:
        out.append({"level": level, "text": text})

    n = int(summary.get("num_trades", 0) or 0)
    wr = float(summary.get("win_rate", 0.0) or 0.0)
    exp = float(summary.get("expectancy_r", 0.0) or 0.0)
    pf = float(analytics.get("profit_factor", 0.0) or 0.0)
    payoff = float(analytics.get("payoff", 0.0) or 0.0)
    avg_win = float(analytics.get("avg_win_r", 0.0) or 0.0)
    avg_loss = abs(float(analytics.get("avg_loss_r", 0.0) or 0.0))
    by_reason = analytics.get("by_reason", {}) or {}
    by_year = analytics.get("by_year", {}) or {}

    if n == 0:
        add("info", "この期間・条件では一度もエントリー条件が揃いませんでした。"
                    "期間を延ばすか、通貨ペアを変えてみてください。")
        return out

    if n < 30:
        add("warn", f"取引数が{n}件と少なめです。結果は運の影響を受けやすいので、"
                    "判断は2年以上（30件以上）を目安にしてください。")

    # --- 総合判定 ---
    if pf >= 1.3 and exp > 0:
        add("good", f"期間トータルで黒字です（PF {pf}、1取引あたり平均 {exp:+.2f}R）。")
    elif pf < 1.0:
        add("bad", f"期間トータルで負け越しです（プロフィットファクター {pf}"
                   f"＝総利益が総損失の{pf}倍しかない）。1取引あたり平均 {exp:+.2f}R。")
    else:
        add("warn", f"かろうじてプラス〜トントン（PF {pf}）。コスト次第でマイナスに転びます。")

    # --- 勝ち負けの「形」（コアの分析） ---
    if payoff and payoff < 1.3 and wr < 0.45:
        add("bad", f"典型的な『利小損大』です。勝率{wr*100:.0f}%と低いのに、"
                   f"勝ち平均{avg_win:.2f}R ÷ 負け平均{avg_loss:.2f}R＝ペイオフ{payoff}しかありません。"
                   "トレンド追随は本来『低勝率でも利大（ペイオフ2以上）』で勝つ設計です。"
                   "利を伸ばせていない＝トレーリング（利食い）が早すぎて、"
                   "伸びる前に切られている可能性が高いです。")
    elif payoff and payoff >= 2.0:
        add("good", f"勝ち平均{avg_win:.2f}R vs 負け平均{avg_loss:.2f}R（ペイオフ{payoff}）＝利大損小の理想形。"
                    f"勝率{wr*100:.0f}%の低さはトレンド追随では正常です。")

    # --- 決済理由の内訳 ---
    stop = by_reason.get("stop")
    trail = by_reason.get("trail")
    tp = by_reason.get("take_profit")
    if stop:
        line = f"損切り(stop) {stop['count']}回で合計{stop['total_r']:+.1f}R"
        if trail:
            line += f"、利益トレール(trail) {trail['count']}回で合計{trail['total_r']:+.1f}R"
        if tp:
            line += f"、部分利確(take_profit) {tp['count']}回で合計{tp['total_r']:+.1f}R"
        add("info", line + "。")
    trail_ct = trail["count"] if trail else 0
    if stop and stop["count"] > max(3, 2 * trail_ct):
        add("warn", "決済の大半が損切りです。エントリー直後に逆行して切られている"
                    "＝『ブレイクのダマシ』を多く掴んでいる疑いがあります。"
                    "エントリーの質を上げる（レンジ回避・ダマシAIフィルタ）と改善する可能性があります。")

    # --- 年別（相場つき依存の可視化） ---
    if len(by_year) >= 2:
        best = max(by_year.items(), key=lambda kv: kv[1]["total_r"])
        worst = min(by_year.items(), key=lambda kv: kv[1]["total_r"])
        if best[1]["total_r"] > 0 and worst[1]["total_r"] < 0:
            add("info", f"年ごとのムラが大きいです（{best[0]}年 {best[1]['total_r']:+.1f}R、"
                        f"{worst[0]}年 {worst[1]['total_r']:+.1f}R）。"
                        "トレンドが出た年は勝ち、レンジの年は負ける＝相場つき依存が強いということです。")

    # --- 改善の方向 ---
    recs: List[str] = []
    if payoff and payoff < 1.5:
        recs.append("トレーリングを緩めて利を伸ばす（ATR_TRAIL_MULT を 2.0→3.0 など）")
    if stop and stop["count"] > max(3, 2 * trail_ct):
        recs.append("『レンジ回避(ADX)』フィルタをON（下のチェックで比較できます）")
        recs.append("『部分利確』をON（先に一部利確して残りを伸ばす）")
    if recs:
        add("info", "改善の方向 → " + " / ".join(recs))
    return out
