# easy-login-system-app

管理者(admin)と利用者(user)がログインできるシンプルな認証システムです。
Flask + SQLAlchemy で実装しています。

## 機能

- ユーザー登録 / ログイン / ログアウト（セッションベース認証）
- パスワードはハッシュ化して保存
- ロールによるアクセス制御（`admin` / `user`）
- 管理者と利用者でログインページを分離
  - 利用者ログイン: `/login`
  - 管理者ログイン: `/admin/login`
  - 相手側のページからログインしようとすると正しいページへ誘導
- 管理者向けユーザー管理画面
  - ユーザー一覧表示
  - ユーザー新規作成（ロール指定可）
  - ロール変更
  - ユーザー削除
  - ※最後の管理者は削除・降格できない安全装置付き

## 初期管理者アカウント

起動時に管理者アカウントが自動作成されます（既存なら何もしません）。

| 項目 | デフォルト | 環境変数 |
| --- | --- | --- |
| ユーザー名 | `admin` | `ADMIN_USERNAME` |
| パスワード | `admin123` | `ADMIN_PASSWORD` |

本番では必ず `ADMIN_PASSWORD` と `SECRET_KEY` を変更してください。

## 起動方法

### Docker Compose（Flask + PostgreSQL）

```bash
docker compose up --build
```

http://localhost:8000 にアクセスします。

### ローカル単体実行（SQLite にフォールバック）

`DATABASE_URL` が未設定の場合は SQLite (`app.db`) を使います。

```bash
pip install -r requirements.txt
python app.py
```

## 環境変数

| 変数 | 説明 |
| --- | --- |
| `DATABASE_URL` | DB 接続先。未設定なら SQLite を使用 |
| `SECRET_KEY` | セッション署名鍵。本番では必ず変更 |
| `ADMIN_USERNAME` | 初期管理者のユーザー名 |
| `ADMIN_PASSWORD` | 初期管理者のパスワード |

---

## AI FX トレーディングエンジン（開発中）

OANDA v20 API を用いた AI 自動売買システムを構築中です。設計の全体像は
[`docs/DESIGN.md`](docs/DESIGN.md) を参照してください。

### 現在の実装（Phase 1: データ取得・指標・分析・バックテスト）

`trading/` パッケージ:

| モジュール | 役割 |
| --- | --- |
| `config.py` | 環境変数からの設定（既定は **practice** 口座） |
| `oanda_client.py` | OANDA v20 REST クライアント（足/口座取得、リトライ付き） |
| `data_feed.py` | ローソク足の正規化・上位足リサンプル |
| `indicators.py` | ATR / ADX / EMA（自前実装、外部TAライブラリ非依存） |
| `analysis.py` | レンジ/トレンド判定・上位足の方向一致(MTF) |
| `strategy.py` | M15ブレイク＋MTF一致＋ATR/出来高確認のエントリー条件 |
| `backtester.py` | ルックアヘッドを避けたイベントドリブン・バックテスト（R倍数評価） |

### バックテストのデモ

```bash
# オフライン（合成データ。OANDA不要）
python scripts/run_backtest.py

# OANDA practice の実データで
OANDA_API_TOKEN=... OANDA_ACCOUNT_ID=... python scripts/run_backtest.py --live-data
```

### テスト

```bash
python tests/test_trading.py     # pytest 不要、ネットワーク非依存
```

> ⚠️ 注意: バックテストの好成績は将来の利益を保証しません。実弁投入は長期の
> フォワードテスト後にしてください。Phase 2 以降で自動発注（デモ口座）・
> 可視化・LLM解析を追加予定です。
