"""トレーディング・ダッシュボード（Flask Blueprint, Phase 3）。

- 資産曲線（R / 損益）
- 取引ログ
- インストルメント別・セッション別の勝率/期待値
- キルスイッチ操作（ブレーカーの JSON フラグを更新し、エンジンが次ティックで全決済）

ログイン必須。OANDA 認証情報が無くても表示できる（DBとブレーカーJSONのみ参照）。
ルートはモジュール読み込み時に一度だけ定義し、register_dashboard() は
各 Flask app への登録のみを行う（create_app の複数回呼び出しに耐える）。
"""
from __future__ import annotations

import json
import os
import threading
from functools import wraps

import pandas as pd

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import metrics
from .config import Settings
from .safety import CircuitBreaker
from .store import TradeStore

trading_bp = Blueprint("trading", __name__, url_prefix="/trading")

BREAKER_STATE_PATH = os.environ.get("BREAKER_STATE_PATH", "instance/breaker.json")


def _store() -> TradeStore:
    return TradeStore()


def _breaker(settings: Settings) -> CircuitBreaker:
    return CircuitBreaker.load(settings, path=BREAKER_STATE_PATH)


def _login_required(view):
    """認証アプリのセッション（user_id）でログインを確認する。"""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


@trading_bp.route("/")
@_login_required
def overview():
    from models import UserSettings

    settings = Settings()
    store = _store()
    closed = store.closed_trades()
    breaker = _breaker(settings)
    us = UserSettings.get_or_create(_current_user_id())
    broker = us.broker or "oanda"
    context = {
        "settings": settings,
        "broker": broker,
        "broker_env": ("paper" if broker == "paper" else us.oanda_env),
        "broker_ready": us.broker_ready,
        "summary": metrics.summary(closed),
        "by_instrument": metrics.group_stats(closed, "instrument"),
        "by_session": metrics.group_stats(closed, "session"),
        "equity": metrics.equity_curve(closed),
        "trades": store.list_trades(limit=100),
        "open_count": store.open_count(),
        "breaker": breaker.state,
    }
    return render_template("trading_dashboard.html", **context)


@trading_bp.route("/kill", methods=["POST"])
@_login_required
def kill():
    breaker = _breaker(Settings())
    breaker.kill()
    flash("キルスイッチをONにしました。エンジンが次ティックで全建玉を決済します。",
          "success")
    return redirect(url_for("trading.overview"))


@trading_bp.route("/reset-kill", methods=["POST"])
@_login_required
def reset_kill():
    breaker = _breaker(Settings())
    breaker.reset_kill()
    flash("キルスイッチを解除しました。", "success")
    return redirect(url_for("trading.overview"))


def _current_user_id():
    return session.get("user_id")


# バックテストの期間プリセット。長い期間ほど計算に時間がかかるため、
# 短い期間を選べばタイムアウトを確実に避けられる（M15は約25000本/年）。
# days: 集計する期間（直近N日）。None は取り込み済み全期間。
# どの期間でも「全データで1回シミュレーション → 直近N日だけ集計」するため、
# 短い期間は必ず長い期間の一部分になり、成績が食い違わない。
_BT_PERIODS = {
    "60d": {"label": "直近60日", "days": 60},
    "3m": {"label": "直近3ヶ月", "days": 90},
    "6m": {"label": "直近6ヶ月", "days": 180},
    "1y": {"label": "直近1年", "days": 365},
    "2y": {"label": "直近2年", "days": 730},
    "4y": {"label": "直近4年", "days": 1460},
    "all": {"label": "全期間（取り込み済みすべて）", "days": None},
}

# バックテスト/取り込みで選べる通貨ペア（HistData 提供の主要ペア）。
# 値動きの異なる複数ペアに分散すると資産曲線がなめらかになる（分散効果）。
_BT_INSTRUMENTS = [
    "USD_JPY", "EUR_USD", "GBP_USD", "EUR_JPY", "GBP_JPY",
    "AUD_USD", "USD_CHF", "USD_CAD", "NZD_USD", "AUD_JPY", "EUR_GBP",
]


def _hist_coverage():
    """取り込み済みの長期データがある通貨ペアと期間を返す。"""
    try:
        from .histdata import HistStore
        store = HistStore()
        out = {}
        for inst in store.instruments():
            mn, mx, cnt = store.coverage(inst, "M15")
            if cnt:
                out[inst] = {"from": (mn or "")[:10], "to": (mx or "")[:10], "count": cnt}
        return out
    except Exception:  # noqa: BLE001
        return {}


@trading_bp.route("/import", methods=["GET"])
@_login_required
def hist_import_view():
    return render_template("trading_import.html", hist=_hist_coverage(),
                          settings=Settings(), instruments=_BT_INSTRUMENTS,
                          status=_read_status())


def _status_path():
    base = os.path.dirname(os.environ.get("HIST_DB_PATH", "instance/histdata.db")) or "instance"
    return os.path.join(base, "import_status.json")


def _write_status(state: str, message: str) -> None:
    try:
        path = _status_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"state": state, "message": message}, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _read_status():
    try:
        with open(_status_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def _run_import(instrument: str, year: int) -> None:
    """バックグラウンドで年次→（不可なら）月次に取り込む。"""
    from .histdata import HistStore, download_month, download_year, import_m1_bytes
    store = HistStore()
    try:
        n = import_m1_bytes(store, instrument, download_year(instrument, year), is_zip=True)
        if n == 0:
            months = 0
            for month in range(1, 13):
                _write_status("running", f"{instrument} {year}年 {month}月を取得中…（{n}本）")
                try:
                    m = import_m1_bytes(store, instrument,
                                        download_month(instrument, year, month), is_zip=True)
                    n += m
                    months += 1 if m > 0 else 0
                except Exception:  # noqa: BLE001
                    continue
            if n > 0:
                _write_status("done", f"{instrument} {year}年 取り込み完了（{months}か月・{n}本のM15）")
            else:
                _write_status("error", f"{instrument} {year}年 データが取得できませんでした。手動アップロードをお試しください。")
        else:
            _write_status("done", f"{instrument} {year}年 取り込み完了（{n}本のM15）")
    except Exception as exc:  # noqa: BLE001
        _write_status("error", f"取り込み失敗: {exc}")


def _import_one_year(store, instrument, year) -> int:
    """1ペア・1年を取り込み、保存本数を返す（年一括→不可なら月別）。"""
    from .histdata import download_month, download_year, import_m1_bytes
    n = 0
    try:
        n = import_m1_bytes(store, instrument, download_year(instrument, year), is_zip=True)
    except Exception:  # noqa: BLE001
        n = 0
    if n == 0:  # 年一括が無い（進行中の年など）→ 月別フォールバック
        for month in range(1, 13):
            try:
                n += import_m1_bytes(store, instrument,
                                     download_month(instrument, year, month), is_zip=True)
            except Exception:  # noqa: BLE001
                continue
    return n


def _run_import_all(instruments, years, current_year) -> None:
    """全ペア×指定年をバックグラウンドで一括取り込み（進捗を逐次書き込む）。

    既に十分な本数が入っている過去年はスキップして再ダウンロードを避ける。
    1件ずつ try するので、一部が失敗しても全体は止まらない。
    """
    from .histdata import HistStore
    store = HistStore()
    total = len(instruments) * len(years)
    step = ok = skipped = failed = 0
    bars = 0
    try:
        for inst in instruments:
            for yr in years:
                step += 1
                # 過去年で既に揃っていればスキップ（今年は増え続けるので毎回更新）
                if yr < current_year and store.year_count(inst, yr) > 20000:
                    skipped += 1
                    continue
                _write_status(
                    "running",
                    f"取り込み中… {inst} {yr}年（{step}/{total}）"
                    f"｜成功{ok}・スキップ{skipped}・失敗{failed}")
                n = _import_one_year(store, inst, yr)
                if n > 0:
                    ok += 1
                    bars += n
                else:
                    failed += 1
        _write_status(
            "done",
            f"一括取り込み完了：取得{ok}件・スキップ{skipped}件・失敗{failed}件"
            f"（{bars:,}本のM15を保存）。失敗分は方法1/2で個別に取り込めます。")
    except Exception as exc:  # noqa: BLE001
        _write_status("error", f"一括取り込みで問題が発生: {exc}")


@trading_bp.route("/import/all", methods=["POST"])
@_login_required
def hist_import_all():
    """全通貨ペア・過去5年ぶんを一括取り込み（バックグラウンド・タイムアウトなし）。"""
    import datetime

    status = _read_status()
    if status and status.get("state") == "running":
        flash("すでに取り込み中です。完了までお待ちください（この画面で進捗が更新されます）。",
              "error")
        return redirect(url_for("trading.hist_import_view"))

    year_now = datetime.datetime.now(datetime.timezone.utc).year
    years = list(range(year_now - 5, year_now + 1))  # 過去5年＋今年
    _write_status("running", "一括取り込みを開始します…")
    threading.Thread(target=_run_import_all,
                     args=(list(_BT_INSTRUMENTS), years, year_now),
                     daemon=True).start()
    flash(f"全{len(_BT_INSTRUMENTS)}ペア・{years[0]}〜{years[-1]}年の取り込みを開始しました。"
          "数分〜十数分かかります。この画面で進捗を確認できます（自動更新）。", "success")
    return redirect(url_for("trading.hist_import_view"))


@trading_bp.route("/import/auto", methods=["POST"])
@_login_required
def hist_import_auto():
    instrument = (request.form.get("instrument") or "USD_JPY").strip()
    try:
        year = int(request.form.get("year") or 0)
    except ValueError:
        year = 0
    if year < 2000 or year > 2100:
        flash("正しい年を入力してください（例: 2024）。", "error")
        return redirect(url_for("trading.hist_import_view"))

    _write_status("running", f"{instrument} {year}年 を取り込み中…")
    # 504（タイムアウト）を避けるためバックグラウンドで実行し、即応答する
    threading.Thread(target=_run_import, args=(instrument, year), daemon=True).start()
    flash("取り込みを開始しました。1〜数分後にこの画面を再読み込みすると進行状況が更新されます。",
          "success")
    return redirect(url_for("trading.hist_import_view"))


@trading_bp.route("/import/upload", methods=["POST"])
@_login_required
def hist_import_upload():
    from .histdata import HistStore, import_m1_bytes

    instrument = (request.form.get("instrument") or "USD_JPY").strip()
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("ファイルを選んでください。", "error")
        return redirect(url_for("trading.hist_import_view"))
    try:
        data = f.read()
        is_zip = f.filename.lower().endswith(".zip")
        n = import_m1_bytes(HistStore(), instrument, data, is_zip=is_zip)
        if n == 0:
            flash("ファイルからデータを読めませんでした（HistDataのM1形式か確認してください）。", "error")
        else:
            flash(f"{instrument}: {f.filename} を取り込みました（{n}本のM15）。", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"取り込みに失敗しました: {exc}", "error")
    return redirect(url_for("trading.hist_import_view"))


@trading_bp.route("/backtest", methods=["GET"])
@_login_required
def backtest_view():
    settings = Settings()
    return render_template(
        "trading_backtest.html", settings=settings, result=None, compare=None,
        hist=_hist_coverage(), periods=_BT_PERIODS, instruments=_BT_INSTRUMENTS,
        per_pair=None,
        form={"instrument": _BT_INSTRUMENTS[0], "period": "60d",
              "spread_pips": 0.8, "slippage_pips": 0.2,
              "f_htf2": False, "f_trail": False, "f_strong": False,
              "f_retest": False, "f_rangeok": False, "f_rangestop": False,
              "f_adx": False, "f_tp": False, "f_ml": False, "trail_mult": 3.0})


@trading_bp.route("/backtest", methods=["POST"])
@_login_required
def backtest_run():
    from .backtester import Backtester
    from .data_feed import candles_to_df

    settings = Settings()
    period_key = request.form.get("period")
    if period_key not in _BT_PERIODS:
        period_key = "60d"
    preset = _BT_PERIODS[period_key]
    # ロジック改良トグル
    f_htf2 = request.form.get("f_htf2") == "on"
    f_trail = request.form.get("f_trail") == "on"
    f_strong = request.form.get("f_strong") == "on"
    f_retest = request.form.get("f_retest") == "on"
    f_rangeok = request.form.get("f_rangeok") == "on"
    f_rangestop = request.form.get("f_rangestop") == "on"
    f_adx = request.form.get("f_adx") == "on"
    f_tp = request.form.get("f_tp") == "on"
    f_ml = request.form.get("f_ml") == "on"
    improved = (f_htf2 or f_trail or f_strong or f_retest or f_rangeok
                or f_rangestop or f_adx or f_tp or f_ml)
    trail_mult = _fnum(request.form.get("trail_mult"), 3.0)
    form = {
        "instrument": (request.form.get("instrument") or settings.instruments[0]).strip(),
        "period": period_key,
        "spread_pips": _fnum(request.form.get("spread_pips"), 0.8),
        "slippage_pips": _fnum(request.form.get("slippage_pips"), 0.2),
        "f_htf2": f_htf2, "f_trail": f_trail, "f_strong": f_strong,
        "f_retest": f_retest, "f_rangeok": f_rangeok, "f_rangestop": f_rangestop,
        "f_adx": f_adx, "f_tp": f_tp, "f_ml": f_ml, "trail_mult": trail_mult,
    }
    error = result = summary = analytics = diagnosis = None
    equity = []
    compare = None
    data_from = data_to = None
    source_used = None
    days = preset.get("days")

    def _make_settings(with_improve):
        s = Settings()
        if with_improve:
            if f_htf2:
                s.htf_granularities = ["H4", "D"]  # 上位足を2つに緩めエントリーを増やす
            if f_trail:
                s.atr_trail_mult = trail_mult  # 利を伸ばす（早すぎる利食いを防ぐ）
            if f_strong:
                s.breakout_body_min = 0.4  # 強いブレイクのみ（弱い/ヒゲ主体を除外）
            if f_retest:
                s.retest_entry = True  # 追いかけず押し戻り（リテスト）を待つ
            if f_rangeok:
                s.range_confirm = True  # 本物のレンジ（複数タッチ＋横ばい）からの放れのみ
            if f_rangestop:
                s.range_stop = True  # 損切りをレンジの反対側の端に置く（構造的ストップ）
            if f_adx:
                s.entry_adx_min = 22.0
            if f_tp:
                s.partial_tp_r = 1.0
        return s
    per_pair = None
    try:
        from .backtester import BacktestResult, diagnose
        inst = form["instrument"]
        cov = _hist_coverage()

        # どの期間でも「全データで1回シミュレーション → 直近N日だけ集計」する。
        # 全期間で指標を暖機してから切り出すので、短い期間は必ず長い期間の一部分になる。
        def _load_df(pair):
            """(df, source) を返す。データが無ければ (None, None)。"""
            cands = []
            src = None
            if pair in cov:
                from .histdata import HistStore
                cands = HistStore().load_candles(pair, "M15", limit=None)
                if cands:
                    src = "hist"
            if not cands and days is not None and days <= 60:
                from .market_data import YahooMarketData
                cands = YahooMarketData().get_candles(
                    pair, settings.trigger_granularity, count=5000, range_override="60d")
                if cands:
                    src = "yahoo"
            if not cands:
                return None, None
            d = candles_to_df(cands)
            if len(d) < max(settings.ema_slow, 60):
                return None, None
            return d, src

        def _cf(d):
            if days is None:
                return None
            c = d.index[-1] - pd.Timedelta(days=days)
            return c if c > d.index[0] else None

        def _run(pair, s, use_ml, d, cf):
            b = Backtester(s, spread_pips=form["spread_pips"],
                           slippage_pips=form["slippage_pips"])
            return b.run(pair, d, count_from=cf, fakeout_ml=use_ml)

        if inst == "__ALL__":
            # --- 全ペア合算（分散効果を見る） ---
            pairs = [p for p in _BT_INSTRUMENTS if p in cov]
            if not pairs:
                error = ("合算するには長期データを取り込んだ通貨ペアが必要です。"
                         "「追加で取り込む」から複数ペアを取り込んでください。")
            else:
                combo = BacktestResult(instrument="ALL")
                per_pair = []
                froms, tos = [], []
                for p in pairs:
                    d, _src = _load_df(p)
                    if d is None:
                        continue
                    cf = _cf(d)
                    r = _run(p, _make_settings(improved), f_ml and improved, d, cf)
                    combo.trades.extend(r.closed)
                    per_pair.append({"instrument": p, **r.summary()})
                    froms.append(cf or d.index[0])
                    tos.append(d.index[-1])
                if not combo.trades:
                    error = "合算対象の取引がありませんでした（データ不足かもしれません）。"
                else:
                    combo.trades.sort(key=lambda t: t.exit_time)
                    result = combo
                    source_used = "hist"
                    summary = combo.summary()
                    analytics = combo.analytics()
                    diagnosis = diagnose(summary, analytics)
                    equity = _equity_curve(combo)
                    per_pair.sort(key=lambda x: x["total_r"], reverse=True)
                    data_from = min(froms).strftime("%Y-%m-%d")
                    data_to = max(tos).strftime("%Y-%m-%d")
        else:
            # --- 単一ペア ---
            df, src = _load_df(inst)
            if df is None:
                error = ("この通貨ペアの長期データが未取り込みです。"
                         "「長期データは未取り込みです → こちらから取り込む」から取り込んでください。")
            else:
                source_used = src
                count_from = _cf(df)
                data_from = (count_from or df.index[0]).strftime("%Y-%m-%d")
                data_to = df.index[-1].strftime("%Y-%m-%d")
                result = _run(inst, _make_settings(improved), f_ml and improved,
                              df, count_from)
                summary = result.summary()
                analytics = result.analytics()
                diagnosis = diagnose(summary, analytics)
                equity = _equity_curve(result)
                if improved:
                    base = _run(inst, _make_settings(False), False, df, count_from)
                    compare = {"base": base.summary(), "improved": summary}
    except Exception as exc:  # noqa: BLE001
        error = f"バックテストに失敗しました: {exc}"

    return render_template(
        "trading_backtest.html", settings=settings, form=form, hist=_hist_coverage(),
        periods=_BT_PERIODS, instruments=_BT_INSTRUMENTS, result=result,
        summary=summary, analytics=analytics, diagnosis=diagnosis, equity=equity,
        error=error, compare=compare, per_pair=per_pair,
        data_from=data_from, data_to=data_to, source_used=source_used)


def _fnum(value, default):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _equity_curve(result):
    """決済順の累積R系列（グラフ用）。"""
    cum = 0.0
    points = []
    for t in sorted(result.closed, key=lambda x: x.exit_time or 0):
        cum += t.r_multiple
        points.append(round(cum, 4))
    return points


@trading_bp.route("/settings", methods=["GET"])
@_login_required
def settings_view():
    from models import UserSettings

    us = UserSettings.get_or_create(_current_user_id())
    return render_template("trading_settings.html", s=us)


@trading_bp.route("/settings", methods=["POST"])
@_login_required
def settings_save():
    from models import UserSettings, db

    us = UserSettings.get_or_create(_current_user_id())
    form = request.form

    # ブローカー選択
    us.broker = form.get("broker") if form.get("broker") in ("oanda", "paper") else "oanda"

    # 秘密情報: 入力があった場合のみ更新（空欄なら既存を維持）
    token = (form.get("oanda_token") or "").strip()
    if token:
        us.set_oanda_token(token)
    akey = (form.get("anthropic_key") or "").strip()
    if akey:
        us.set_anthropic_key(akey)
    # 明示的なクリア
    if form.get("clear_oanda_token"):
        us.oanda_token_enc = None
    if form.get("clear_anthropic_key"):
        us.anthropic_key_enc = None

    # 非秘密の設定
    us.oanda_account_id = (form.get("oanda_account_id") or "").strip() or None
    us.oanda_env = form.get("oanda_env") if form.get("oanda_env") in ("practice", "live") else "practice"
    us.instruments = (form.get("instruments") or "USD_JPY,EUR_USD").strip()
    us.risk_per_trade = _num(form.get("risk_per_trade"), 0.5)
    us.max_open_positions = int(_num(form.get("max_open_positions"), 2))
    us.econ_calendar_url = (form.get("econ_calendar_url") or "").strip() or None
    us.econ_blackout_before_min = int(_num(form.get("econ_blackout_before_min"), 30))
    us.econ_blackout_after_min = int(_num(form.get("econ_blackout_after_min"), 15))

    us.enable_news = bool(form.get("enable_news"))
    us.enable_ml = bool(form.get("enable_ml"))
    us.enable_council = bool(form.get("enable_council"))
    us.enable_calendar = bool(form.get("enable_calendar"))
    us.engine_enabled = bool(form.get("engine_enabled"))

    # live を選ぶには OANDA トークンが必須（安全装置）
    if us.oanda_env == "live" and not us.has_oanda_token:
        us.oanda_env = "practice"
        flash("OANDAトークン未設定のため practice に戻しました。", "error")

    db.session.commit()
    flash("設定を保存しました。", "success")
    return redirect(url_for("trading.settings_view"))


def _num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def register_dashboard(app, login_required=None):
    """app に Blueprint を登録する（ルート定義はモジュール読込時に完了済み）。

    login_required は後方互換のため受け取るが未使用（内部で session を確認）。
    """
    app.register_blueprint(trading_bp)
