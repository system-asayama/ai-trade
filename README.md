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

### ブローカー選択（ペーパー / OANDA）

対応ブローカーを **設定画面から選べます**（`broker`）。どちらも同一インターフェース
（`trading/broker.py` のファクトリで切替）で、エンジン側は差し替えを意識しません。

| ブローカー | 実装 | 備考 |
| --- | --- | --- |
| **ペーパー（リアル価格）** | `paper_broker.py` + `market_data.py` | **口座・入金・本人確認 不要**。無料のリアル価格（Yahoo Finance）でロボットを動かし、注文は仮想でシミュレーション。まず試すのに最適 |
| OANDA v20 | `oanda_client.py` | practice/live。トークン＋口座ID |

**ペーパートレード**は価格だけ本物を使い（データ閲覧は口座不要）、約定は仮想です。
`settle()` で各建玉のストップ到達を判定して決済し、結果はダッシュボードに溜まります。
※約定は仮想のためスプレッド/スリッページは再現されず、実弾の成績を保証しません。

### マルチテナント（各ユーザーがAPIキーを持ち込み）

ユーザー（法人）ごとに、自分の OANDA / Anthropic のAPIキーと設定を
**Web画面 `/trading/settings` から登録**できます。

- APIキーは `cryptobox`（標準ライブラリのみのHMAC-SHA256認証付き暗号）で
  **暗号化して保存**。画面に生のキーは表示されません
- 各ユーザーの設定は `models.UserSettings` に保存され、`trading/tenant.py` が
  そこから `Settings` とエンジンを組み立てます
- 常駐ランナー `scripts/run_multi.py` が `engine_enabled` の全ユーザーを
  それぞれの設定・キーで実行します

```bash
python scripts/run_multi.py --poll 60   # 有効化ユーザーを60秒毎に実行
```

暗号化鍵は `APP_ENCRYPTION_KEY`（未設定なら `SECRET_KEY` から導出）。本番では
必ずランダムな値を設定してください。

### 本番での常駐（docker-compose）

`docker-compose.yml` に **`worker` サービス**を追加済みです。`web` と同一イメージ・
同一の `instance/` ボリューム（取引DB・キルスイッチ状態を共有）で
`scripts/run_multi.py` を常駐実行します。`main` への push で自動デプロイされ、
`restart: unless-stopped` で常時稼働します。

```bash
docker compose up -d          # web + worker + db が起動
docker compose logs -f worker # ワーカーのログ
```

重要:
- `web` と `worker` の `APP_ENCRYPTION_KEY`（無ければ `SECRET_KEY`）は**必ず同一**に
  してください。異なると worker がユーザーの保存済みキーを復号できません。
- 稼働間隔は `WORKER_POLL_SECONDS`（既定60秒）で調整できます。

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

### Phase 2: 自動執行・リスク・安全装置（デモ口座）

| モジュール | 役割 |
| --- | --- |
| `risk.py` | 許容リスク(%)とストップ幅から建玉数(units)を逆算 |
| `executor.py` | OANDA への成行＋SL発注・ATRトレーリング・全決済 |
| `safety.py` | サーキットブレーカー（日次損失/連敗/同時保有数）＋キルスイッチ（JSON永続化） |
| `engine.py` | 1ティック処理（決済反映→安全判定→シグナル→発注→トレーリング）と常駐ループ |

```bash
# 1ティックだけ実行（OANDA practice）
OANDA_API_TOKEN=... OANDA_ACCOUNT_ID=... python scripts/run_engine.py --once

# 常駐（60秒間隔）
python scripts/run_engine.py --poll 60

# 緊急停止（全建玉決済＋新規停止）/ 解除 / 状態
python scripts/run_engine.py --kill
python scripts/run_engine.py --reset-kill
python scripts/run_engine.py --status
```

### Phase 3: 永続化・ダッシュボード・統計

| モジュール | 役割 |
| --- | --- |
| `store.py` | 取引の永続化（sqlite。open/close 記録、R倍数自動計算） |
| `metrics.py` | 勝率・期待値・最大DD・資産曲線・セッション分類の集計 |
| `dashboard.py` | Flask Blueprint。ログイン後 `/trading` で可視化＋キルスイッチ操作 |

Web の `/trading` で資産曲線（累積R）・取引ログ・ペア別/時間帯別の勝率を表示し、
ブラウザから緊急停止（キルスイッチ）も操作できます（フラグを更新し、エンジンが
次ティックで全決済）。エンジンは約定/決済を自動で `store` に記録します。

### Phase 4: ニュース/中銀発言の解析（Claude API）

| モジュール | 役割 |
| --- | --- |
| `news.py` | Claude（`claude-opus-4-8`、構造化出力）でニュース/中銀発言を解析し、通貨ペアへの方向バイアス・リスク度・確信度をスコア化。エントリー可否とロットサイズの補助フィルタを提供 |

エンジンに `news_provider` を渡すと、エントリー前にニュースセンチメントで判定します：
**高リスク材料（指標・中銀直後）は見送り**、**高確信の逆風は見送り**、弱い逆風は**サイズ縮小**、
追い風は通常サイズ。`news_provider` 未設定なら従来どおり（フィルタ無効）。

```bash
# 見出しを解析（ANTHROPIC_API_KEY が必要）
python scripts/analyze_news.py USD_JPY "FRBがタカ派姿勢を強調、追加利上げを示唆"
```

### Phase 5: ダマシ予測ML・チャート画像認識・AI合議

| モジュール | 役割 |
| --- | --- |
| `ml.py` | ダマシブレイク確率予測。純numpyのロジスティック回帰（依存追加なし）。取引ログから学習し、成功確率が低い場面を見送り |
| `vision.py` | Claude vision でローソク足チャート画像を解析し、トレンド/パターン/ダマシリスクを構造化出力 |
| `council.py` | 複数の Claude アナリスト（テクニカル/マクロ/リスク）が賛否を投票し、多数決でエントリー可否・サイズを決定 |

エンジンに `fakeout_model` / `council` を渡すと、ニュースフィルタに続けて
**ダマシ予測ゲート**（成功確率 < `FAKEOUT_MIN_PROBA` で見送り）と
**AI合議ゲート**（多数決で見送り/サイズ調整）が働きます（いずれも任意）。

```bash
# 取引ログからダマシ予測モデルを学習
python scripts/train_model.py
```

### 経済指標カレンダー（危険度フィルタ）

| モジュール | 役割 |
| --- | --- |
| `calendar.py` | 経済指標カレンダーを取得し、対象通貨の高重要度イベント前後を**ブラックアウト**（エントリー見送り）。プロバイダ差し替え可能（Static / 汎用HTTP JSON） |

`ECON_CALENDAR_URL` を設定すると、engine が高重要度イベントの**前 `ECON_BLACKOUT_BEFORE_MIN` 分〜後 `ECON_BLACKOUT_AFTER_MIN` 分**の窓で新規エントリーを見送ります（最重要のハードフィルタとして他ゲートより先に評価）。JSON のフィールド名は `ECON_CALENDAR_FIELD_*` で調整できます。

```bash
# カレンダーを取得して確認
ECON_CALENDAR_URL=https://example.com/calendar.json python scripts/fetch_calendar.py
```

### バックテストのデモ

```bash
# オフライン（合成データ。OANDA不要）
python scripts/run_backtest.py

# OANDA practice の実データで
OANDA_API_TOKEN=... OANDA_ACCOUNT_ID=... python scripts/run_backtest.py --live-data
```

### テスト

```bash
python tests/test_trading.py     # Phase 1（指標・分析・バックテスト）
python tests/test_execution.py   # Phase 2（執行・リスク・安全装置・エンジン）
python tests/test_store.py       # Phase 3（永続化・統計・ダッシュボード）
python tests/test_news.py        # Phase 4（ニュース/中銀発言の解析・フィルタ）
python tests/test_ml.py          # Phase 5（ダマシ予測ML・画像認識・AI合議）
python tests/test_calendar.py    # 経済指標カレンダー（危険度フィルタ）
```

> ⚠️ 注意: バックテストの好成績は将来の利益を保証しません。実弁(live)投入は長期の
> フォワードテスト後にしてください。既定は practice 口座で、live はサーキット
> ブレーカー/キルスイッチを通過した場合のみ発注されます。Phase 3 以降で
> 可視化ダッシュボード・LLMニュース解析を追加予定です。
