"""マルチテナント設定（暗号化・per-userSettings・Web設定画面）のテスト。

APIキーの保存時暗号化と、Webからの設定保存を検証する。
`python tests/test_settings.py` で実行可能。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 暗号化鍵を安定させる（cryptobox は SECRET_KEY から鍵導出）
os.environ.setdefault("SECRET_KEY", "test-secret-key-fixed")
# 主DBを一時ファイルに（UserSettings 保存用）
_DB = "/tmp/claude-0/settings_test.db"
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

from trading import cryptobox  # noqa: E402
from trading.tenant import settings_from_user  # noqa: E402


# --- 暗号化 ----------------------------------------------------------------
def test_cryptobox_roundtrip():
    token = "sk-ant-abc123-秘密キー"
    enc = cryptobox.encrypt(token)
    assert enc != token
    assert token not in enc  # 平文が混じらない
    assert cryptobox.decrypt(enc) == token


def test_cryptobox_none_and_empty():
    assert cryptobox.encrypt(None) is None
    assert cryptobox.decrypt(None) == ""
    assert cryptobox.decrypt("") == ""


def test_cryptobox_tamper_detected():
    enc = cryptobox.encrypt("hello")
    tampered = ("A" if enc[0] != "A" else "B") + enc[1:]
    try:
        cryptobox.decrypt(tampered)
        assert False, "改ざんが検知されるべき"
    except ValueError:
        pass


# --- per-user Settings -----------------------------------------------------
class _StubUS:
    """UserSettings 相当のスタブ（DB不要）。"""
    def __init__(self):
        self.oanda_account_id = "001-009-1234567-001"
        self.oanda_env = "practice"
        self.instruments = "USD_JPY, GBP_JPY"
        self.risk_per_trade = 1.0
        self.max_open_positions = 3
        self.econ_blackout_before_min = 45
        self.econ_blackout_after_min = 20

    def get_oanda_token(self):
        return "tok-123"

    def get_anthropic_key(self):
        return "sk-ant-xyz"


def test_settings_from_user_overrides():
    s = settings_from_user(_StubUS())
    assert s.oanda_api_token == "tok-123"
    assert s.oanda_account_id == "001-009-1234567-001"
    assert s.instruments == ["USD_JPY", "GBP_JPY"]
    assert s.risk_per_trade == 1.0
    assert s.max_open_positions == 3
    assert s.econ_blackout_before_min == 45


def test_settings_from_user_rejects_bad_env():
    us = _StubUS()
    us.oanda_env = "bogus"
    assert settings_from_user(us).oanda_env == "practice"


# --- モデルの暗号化アクセサ --------------------------------------------------
def test_model_secret_accessors():
    from models import UserSettings
    us = UserSettings()
    assert not us.has_oanda_token
    us.set_oanda_token("mytoken")
    assert us.has_oanda_token
    assert us.oanda_token_enc != "mytoken"
    assert us.get_oanda_token() == "mytoken"
    us.set_anthropic_key("sk-ant-key")
    assert us.get_anthropic_key() == "sk-ant-key"


# --- Web 設定画面 ----------------------------------------------------------
def _client():
    import app as app_module
    flask_app = app_module.create_app()
    return flask_app.test_client()


def test_settings_requires_login():
    c = _client()
    r = c.get("/trading/settings")
    assert r.status_code in (301, 302)
    assert "/login" in r.headers.get("Location", "")


def test_settings_save_and_persist():
    c = _client()
    c.post("/admin/login", data={"username": "admin", "password": "admin123"})

    r = c.post("/trading/settings", data={
        "oanda_token": "SECRET-TOKEN-XYZ",
        "oanda_account_id": "001-009-9999999-001",
        "oanda_env": "practice",
        "instruments": "USD_JPY,EUR_USD",
        "risk_per_trade": "0.8",
        "max_open_positions": "2",
        "anthropic_key": "sk-ant-secret",
        "enable_news": "on",
        "engine_enabled": "on",
    }, follow_redirects=True)
    assert r.status_code == 200

    # 再表示: 設定済みバッジが出て、生のトークンはHTMLに露出しない
    page = c.get("/trading/settings").get_data(as_text=True)
    assert "設定済み" in page
    assert "SECRET-TOKEN-XYZ" not in page
    assert "sk-ant-secret" not in page


def test_settings_live_without_token_forced_practice():
    c = _client()
    c.post("/admin/login", data={"username": "admin", "password": "admin123"})
    # トークン未設定で live を選んでも practice に戻る
    c.post("/trading/settings", data={
        "oanda_env": "live", "instruments": "USD_JPY",
        "risk_per_trade": "0.5", "max_open_positions": "1",
    }, follow_redirects=True)
    from models import UserSettings
    import app as app_module
    with app_module.app.app_context():
        us = UserSettings.query.first()
        # トークンを入れていないので live にはならない
        assert us.oanda_env == "practice"


def _run_all():
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    code = 1 if _run_all() else 0
    if os.path.exists(_DB):
        os.remove(_DB)
    sys.exit(code)
