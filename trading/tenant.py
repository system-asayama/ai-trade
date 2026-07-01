"""マルチテナント: ユーザーごとの設定からエンジン構成を組み立てる。

各ユーザー（法人）が Web で登録した設定（models.UserSettings）を、
trading.config.Settings と各クライアントに反映する。秘密情報は
UserSettings のアクセサ経由で復号して使う。
"""
from __future__ import annotations

from typing import Any, Optional

from .config import OANDA_HOSTS, Settings


def settings_from_user(us: Any) -> Settings:
    """UserSettings から trading.config.Settings を構築する。

    環境変数由来のデフォルトを生成後、ユーザー値で上書きする。
    """
    s = Settings()
    s.broker = getattr(us, "broker", "oanda") or "oanda"
    s.oanda_api_token = us.get_oanda_token()
    s.oanda_account_id = us.oanda_account_id or ""
    env = (us.oanda_env or "practice").lower()
    s.oanda_env = env if env in OANDA_HOSTS else "practice"

    # Capital.com
    if hasattr(us, "get_capital_api_key"):
        s.capital_api_key = us.get_capital_api_key()
        s.capital_password = us.get_capital_password()
        s.capital_identifier = us.capital_identifier or ""
        s.capital_env = (us.capital_env or "demo").lower()

    if us.instruments:
        s.instruments = [i.strip() for i in us.instruments.split(",") if i.strip()]
    if us.risk_per_trade is not None:
        s.risk_per_trade = float(us.risk_per_trade)
    if us.max_open_positions is not None:
        s.max_open_positions = int(us.max_open_positions)
    if us.econ_blackout_before_min is not None:
        s.econ_blackout_before_min = int(us.econ_blackout_before_min)
    if us.econ_blackout_after_min is not None:
        s.econ_blackout_after_min = int(us.econ_blackout_after_min)
    return s


def build_user_engine(us: Any):
    """UserSettings から TradingEngine を組み立てる（有効化された機能のみ）。

    anthropic / calendar 等は、そのユーザーの鍵・URL が揃っている場合のみ有効化。
    実際の発注には OANDA トークンが必須。
    """
    from .broker import make_broker_client
    from .engine import TradingEngine
    from .safety import CircuitBreaker
    from .store import TradeStore

    settings = settings_from_user(us)
    client = make_broker_client(settings)
    breaker = CircuitBreaker(settings)
    store = TradeStore()  # 単一DB。将来はユーザー別スコープ化を検討

    news_provider = None
    council = None
    calendar = None

    anthropic_key = us.get_anthropic_key()
    anthropic_client = None
    if anthropic_key and (us.enable_news or us.enable_council):
        anthropic_client = _anthropic_client(anthropic_key)

    if us.enable_news and anthropic_client is not None:
        from .news import NewsAnalyzer, SentimentStore
        news_provider = SentimentStore(analyzer=NewsAnalyzer(client=anthropic_client))

    if us.enable_council and anthropic_client is not None:
        from .council import Council
        council = Council(client=anthropic_client)

    fakeout_model = None
    if us.enable_ml:
        fakeout_model = _load_model()

    if us.enable_calendar and us.econ_calendar_url:
        calendar = _build_calendar(settings, us.econ_calendar_url)

    return TradingEngine(
        settings, client, breaker=breaker, store=store,
        news_provider=news_provider, fakeout_model=fakeout_model,
        council=council, calendar=calendar,
    )


def _anthropic_client(api_key: str) -> Optional[Any]:
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return None
    return anthropic.Anthropic(api_key=api_key)


def _load_model():
    import os

    from .ml import FakeoutModel
    path = os.environ.get("FAKEOUT_MODEL_PATH", "instance/fakeout_model.json")
    if os.path.exists(path):
        try:
            return FakeoutModel.load(path)
        except Exception:  # noqa: BLE001
            return None
    return None


def _build_calendar(settings: Settings, url: str):
    from .calendar import EconomicCalendar, HttpCalendarProvider
    return EconomicCalendar(HttpCalendarProvider(url=url), settings)
