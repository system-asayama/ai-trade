"""データベースモデル定義。"""
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# 利用可能なロール（権限）
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)


class User(db.Model):
    """ログインユーザー。admin / user の2種類のロールを持つ。"""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_USER)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<User {self.username} ({self.role})>"


class UserSettings(db.Model):
    """ユーザー（法人）ごとのトレード設定。各自が自分のAPIキーを持ち込む。

    秘密情報（OANDA/Anthropic のキー）は cryptobox で暗号化して保存する。
    """

    __tablename__ = "user_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    # --- ブローカー選択（oanda / capital） ---
    broker = db.Column(db.String(16), default="oanda")

    # --- OANDA（売買の土台） ---
    oanda_token_enc = db.Column(db.Text)          # 暗号化保存
    oanda_account_id = db.Column(db.String(64))
    oanda_env = db.Column(db.String(16), default="practice")

    # --- Capital.com ---
    capital_api_key_enc = db.Column(db.Text)      # 暗号化保存
    capital_password_enc = db.Column(db.Text)     # 暗号化保存
    capital_identifier = db.Column(db.String(128))  # ログインID/メール
    capital_env = db.Column(db.String(16), default="demo")

    # --- 取引対象・リスク ---
    instruments = db.Column(db.String(255), default="USD_JPY,EUR_USD")
    risk_per_trade = db.Column(db.Float, default=0.5)
    max_open_positions = db.Column(db.Integer, default=2)

    # --- Claude（ニュース/中銀解析・AI合議） ---
    anthropic_key_enc = db.Column(db.Text)        # 暗号化保存

    # --- 経済指標カレンダー ---
    econ_calendar_url = db.Column(db.String(512))
    econ_blackout_before_min = db.Column(db.Integer, default=30)
    econ_blackout_after_min = db.Column(db.Integer, default=15)

    # --- 機能トグル ---
    enable_news = db.Column(db.Boolean, default=False)
    enable_ml = db.Column(db.Boolean, default=False)
    enable_council = db.Column(db.Boolean, default=False)
    enable_calendar = db.Column(db.Boolean, default=False)
    # このユーザーのボットを常駐ランナーで動かすか
    engine_enabled = db.Column(db.Boolean, default=False)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("settings", uselist=False))

    # -- 秘密情報のアクセサ（暗号化/復号） --------------------------------
    def set_oanda_token(self, plaintext: str) -> None:
        from trading.cryptobox import encrypt
        self.oanda_token_enc = encrypt(plaintext) if plaintext else None

    def get_oanda_token(self) -> str:
        from trading.cryptobox import decrypt
        return decrypt(self.oanda_token_enc)

    def set_anthropic_key(self, plaintext: str) -> None:
        from trading.cryptobox import encrypt
        self.anthropic_key_enc = encrypt(plaintext) if plaintext else None

    def get_anthropic_key(self) -> str:
        from trading.cryptobox import decrypt
        return decrypt(self.anthropic_key_enc)

    def set_capital_api_key(self, plaintext: str) -> None:
        from trading.cryptobox import encrypt
        self.capital_api_key_enc = encrypt(plaintext) if plaintext else None

    def get_capital_api_key(self) -> str:
        from trading.cryptobox import decrypt
        return decrypt(self.capital_api_key_enc)

    def set_capital_password(self, plaintext: str) -> None:
        from trading.cryptobox import encrypt
        self.capital_password_enc = encrypt(plaintext) if plaintext else None

    def get_capital_password(self) -> str:
        from trading.cryptobox import decrypt
        return decrypt(self.capital_password_enc)

    @property
    def has_oanda_token(self) -> bool:
        return bool(self.oanda_token_enc)

    @property
    def has_capital_key(self) -> bool:
        return bool(self.capital_api_key_enc)

    @property
    def has_anthropic_key(self) -> bool:
        return bool(self.anthropic_key_enc)

    @property
    def broker_ready(self) -> bool:
        """選択中ブローカーの接続情報が揃っているか。"""
        broker = self.broker or "oanda"
        if broker == "paper":
            return True  # ペーパーは口座・鍵不要
        if broker == "capital":
            return bool(self.capital_api_key_enc and self.capital_password_enc
                        and self.capital_identifier)
        return self.has_oanda_token

    @classmethod
    def get_or_create(cls, user_id: int) -> "UserSettings":
        obj = cls.query.filter_by(user_id=user_id).first()
        if obj is None:
            obj = cls(user_id=user_id)
            db.session.add(obj)
            db.session.commit()
        return obj
