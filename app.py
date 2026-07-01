"""管理者と利用者がログインできるシンプルな認証システム。

- セッションベースの認証
- パスワードはハッシュ化して保存
- admin / user のロールによるアクセス制御
- 管理者はユーザー一覧・作成・ロール変更・削除が可能
"""
import os
from functools import wraps

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from models import ROLE_ADMIN, ROLE_USER, ROLES, User, db


def _normalize_db_url(url: str) -> str:
    # SQLAlchemy は postgres:// を認識しないため postgresql:// に変換
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        app.config["SQLALCHEMY_DATABASE_URI"] = _normalize_db_url(database_url)
    else:
        # DATABASE_URL が無い場合はローカル SQLite にフォールバック
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        _init_db_with_retry()
        _ensure_schema()
        _seed_admin()

    _register_routes(app)

    # トレーディング・ダッシュボード（Phase 3）を登録
    try:
        from trading.dashboard import register_dashboard

        register_dashboard(app, login_required)
    except Exception as exc:  # noqa: BLE001 トレーディング機能が無くても認証は動かす
        app.logger.warning("トレーディングダッシュボードの登録に失敗: %s", exc)

    return app


def _init_db_with_retry(max_attempts: int = 10, delay: float = 3.0) -> None:
    """DB(Postgres)がまだ起動していない場合に備え、create_all をリトライする。

    起動直後は DB コンテナが未準備のことがあり、そのまま失敗すると web が
    クラッシュループ（= 502/接続拒否）になる。数回待って再試行する。
    """
    import time

    from sqlalchemy.exc import OperationalError

    for attempt in range(max_attempts):
        try:
            db.create_all()
            return
        except OperationalError as exc:  # DB 未準備など
            if attempt == max_attempts - 1:
                raise
            print(f"[startup] DB未準備、{delay}秒後に再試行 ({attempt + 1}/{max_attempts}): {exc}")
            time.sleep(delay)


def _ensure_schema() -> None:
    """既存テーブルに不足している列を補う（create_all は列を追加しないため）。

    本番DBにモデルへ後から追加した列（broker 等）が無いと 500 になるのを防ぐ。
    失敗しても起動は継続する。
    """
    from sqlalchemy import inspect, text

    wanted = {"broker": "VARCHAR(16)"}
    try:
        insp = inspect(db.engine)
        if "user_settings" not in insp.get_table_names():
            return
        existing = {c["name"] for c in insp.get_columns("user_settings")}
        for name, ddl in wanted.items():
            if name not in existing:
                db.session.execute(
                    text(f"ALTER TABLE user_settings ADD COLUMN {name} {ddl}"))
                print(f"[startup] user_settings に列を追加: {name}")
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 マイグレーション失敗でも起動は続ける
        print(f"[startup] スキーマ補正をスキップ: {exc}")
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _seed_admin() -> None:
    """初期管理者アカウントを作成する（既に存在する場合は何もしない）。"""
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")

    if User.query.filter_by(username=admin_username).first() is None:
        admin = User(username=admin_username, role=ROLE_ADMIN)
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()


# ---------------------------------------------------------------------------
# 認証ヘルパー
# ---------------------------------------------------------------------------
def current_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return db.session.get(User, user_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("ログインが必要です。", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            flash("管理者ログインが必要です。", "error")
            return redirect(url_for("admin_login"))
        if not user.is_admin:
            flash("管理者権限が必要です。", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    @app.context_processor
    def inject_user():
        return {"current_user": current_user()}

    @app.route("/")
    def index():
        if current_user() is not None:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        """利用者（user ロール）の新規登録。"""
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""

            if not username or not password:
                flash("ユーザー名とパスワードを入力してください。", "error")
            elif password != confirm:
                flash("パスワードが一致しません。", "error")
            elif User.query.filter_by(username=username).first() is not None:
                flash("そのユーザー名は既に使われています。", "error")
            else:
                user = User(username=username, role=ROLE_USER)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash("登録が完了しました。ログインしてください。", "success")
                return redirect(url_for("login"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """利用者用ログインページ。"""
        if current_user() is not None:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            user = User.query.filter_by(username=username).first()
            if user is not None and user.check_password(password):
                if user.is_admin:
                    # 管理者は管理者用ログインを使う
                    flash("管理者は管理者ログインページからログインしてください。", "error")
                    return redirect(url_for("admin_login"))
                session.clear()
                session["user_id"] = user.id
                flash(f"ようこそ、{user.username} さん。", "success")
                return redirect(url_for("dashboard"))

            flash("ユーザー名またはパスワードが正しくありません。", "error")

        return render_template("login.html")

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        """管理者用ログインページ。"""
        user = current_user()
        if user is not None:
            return redirect(url_for("admin_users" if user.is_admin else "dashboard"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            user = User.query.filter_by(username=username).first()
            if user is not None and user.check_password(password):
                if not user.is_admin:
                    # 一般利用者はこのページからログインできない
                    flash("このページは管理者専用です。利用者ログインをご利用ください。", "error")
                    return redirect(url_for("login"))
                session.clear()
                session["user_id"] = user.id
                flash(f"管理者としてログインしました（{user.username}）。", "success")
                return redirect(url_for("admin_users"))

            flash("ユーザー名またはパスワードが正しくありません。", "error")

        return render_template("admin_login.html")

    @app.route("/logout")
    def logout():
        was_admin = (current_user() or None) and current_user().is_admin
        session.clear()
        flash("ログアウトしました。", "success")
        return redirect(url_for("admin_login" if was_admin else "login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = current_user()
        # 一般ユーザーは空のダッシュボードを飛ばして「トレード」画面へ
        if not user.is_admin:
            try:
                return redirect(url_for("trading.overview"))
            except Exception:  # noqa: BLE001 トレード機能未登録時は従来画面へ
                pass
        return render_template("dashboard.html", user=user)

    # --- 管理者専用 ------------------------------------------------------
    @app.route("/admin/users")
    @admin_required
    def admin_users():
        users = User.query.order_by(User.created_at.asc()).all()
        return render_template("admin_users.html", users=users, roles=ROLES)

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_create_user():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = request.form.get("role") or ROLE_USER

        if role not in ROLES:
            role = ROLE_USER

        if not username or not password:
            flash("ユーザー名とパスワードを入力してください。", "error")
        elif User.query.filter_by(username=username).first() is not None:
            flash("そのユーザー名は既に使われています。", "error")
        else:
            user = User(username=username, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"ユーザー「{username}」を作成しました。", "success")

        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/role", methods=["POST"])
    @admin_required
    def admin_update_role(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            flash("ユーザーが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        new_role = request.form.get("role")
        if new_role not in ROLES:
            flash("無効なロールです。", "error")
            return redirect(url_for("admin_users"))

        # 最後の管理者を降格させないよう保護
        if user.is_admin and new_role != ROLE_ADMIN and _admin_count() <= 1:
            flash("最後の管理者の権限は変更できません。", "error")
            return redirect(url_for("admin_users"))

        user.role = new_role
        db.session.commit()
        flash(f"「{user.username}」のロールを {new_role} に変更しました。", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_user(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            flash("ユーザーが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        if user.id == current_user().id:
            flash("自分自身は削除できません。", "error")
            return redirect(url_for("admin_users"))

        if user.is_admin and _admin_count() <= 1:
            flash("最後の管理者は削除できません。", "error")
            return redirect(url_for("admin_users"))

        db.session.delete(user)
        db.session.commit()
        flash(f"ユーザー「{user.username}」を削除しました。", "success")
        return redirect(url_for("admin_users"))


def _admin_count() -> int:
    return User.query.filter_by(role=ROLE_ADMIN).count()


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
