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

import os
from functools import wraps

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


# 長期バックテストで一度に処理する M15 本数の上限（およそ2年ぶん）。
# Webのタイムアウト内に収めるためのガード。
_LONG_MAX_BARS = 200000


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
                          settings=Settings())


@trading_bp.route("/import/auto", methods=["POST"])
@_login_required
def hist_import_auto():
    from .histdata import HistStore, download_month, download_year, import_m1_bytes

    instrument = (request.form.get("instrument") or "USD_JPY").strip()
    try:
        year = int(request.form.get("year") or 0)
    except ValueError:
        year = 0
    if year < 2000 or year > 2100:
        flash("正しい年を入力してください（例: 2024）。", "error")
        return redirect(url_for("trading.hist_import_view"))

    store = HistStore()
    try:
        # まず「1年まとめ」を試す（過去の完結した年はこれで取れる）
        n = import_m1_bytes(store, instrument,
                            download_year(instrument, year), is_zip=True)
        if n > 0:
            flash(f"{instrument} {year}年 を取り込みました（{n}本のM15）。", "success")
        else:
            # 取れない年（進行中の年など）は月ごとに取得する
            months = 0
            for month in range(1, 13):
                try:
                    m = import_m1_bytes(store, instrument,
                                        download_month(instrument, year, month), is_zip=True)
                    n += m
                    if m > 0:
                        months += 1
                except Exception:  # noqa: BLE001 その月が無ければスキップ
                    continue
            if n > 0:
                flash(f"{instrument} {year}年 を月別に取り込みました"
                      f"（{months}か月・{n}本のM15）。", "success")
            else:
                flash("データが取得できませんでした。手動ダウンロード→"
                      "アップロードをお試しください。", "error")
    except Exception as exc:  # noqa: BLE001
        flash(f"自動取り込みに失敗しました: {exc} … 手動アップロードをお試しください。", "error")
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
        "trading_backtest.html", settings=settings, result=None,
        hist=_hist_coverage(),
        form={"instrument": settings.instruments[0], "period": "60d",
              "spread_pips": 0.8, "slippage_pips": 0.2})


@trading_bp.route("/backtest", methods=["POST"])
@_login_required
def backtest_run():
    from .backtester import Backtester
    from .data_feed import candles_to_df

    settings = Settings()
    form = {
        "instrument": (request.form.get("instrument") or settings.instruments[0]).strip(),
        "period": request.form.get("period") if request.form.get("period") in ("60d", "long") else "60d",
        "spread_pips": _fnum(request.form.get("spread_pips"), 0.8),
        "slippage_pips": _fnum(request.form.get("slippage_pips"), 0.2),
    }
    error = result = summary = None
    equity = []
    data_from = data_to = None
    try:
        if form["period"] == "long":
            from .histdata import HistStore
            candles = HistStore().load_candles(form["instrument"], "M15",
                                               limit=_LONG_MAX_BARS)
            if not candles:
                error = ("この通貨ペアの長期データが未取り込みです。"
                         "「長期データは未取り込みです → こちらから取り込む」から取り込んでください。")
        else:
            from .market_data import YahooMarketData
            candles = YahooMarketData().get_candles(
                form["instrument"], settings.trigger_granularity,
                count=5000, range_override="60d")

        if error is None:
            df = candles_to_df(candles)
            if len(df) < max(settings.ema_slow, 60):
                error = "過去データが不足しています（別の通貨ペア/期間をお試しください）。"
            else:
                data_from = df.index[0].strftime("%Y-%m-%d")
                data_to = df.index[-1].strftime("%Y-%m-%d")
                bt = Backtester(settings, spread_pips=form["spread_pips"],
                                slippage_pips=form["slippage_pips"])
                result = bt.run(form["instrument"], df)
                summary = result.summary()
                equity = _equity_curve(result)
    except Exception as exc:  # noqa: BLE001
        error = f"バックテストに失敗しました: {exc}"

    return render_template(
        "trading_backtest.html", settings=settings, form=form, hist=_hist_coverage(),
        result=result, summary=summary, equity=equity, error=error,
        data_from=data_from, data_to=data_to)


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
