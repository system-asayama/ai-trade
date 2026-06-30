# AI FXトレーディングシステム 設計書

> 対象リポジトリ: `ai-trade`
> 取引基盤: **OANDA v20 REST / Streaming API**（まずデモ口座 = practice 環境）
> ステータス: ドラフト（実装前の設計フェーズ）

---

## 0. このドキュメントの目的

「AIが相場を分析し、条件が揃えば自動エントリーし、ポジションを管理し、勝敗から学習する」FX自動売買システムを構築するための設計を定義する。
コードはまだ書かず、**何を・どの順で・どう作るか**を確定させることがゴール。

### 重要な前提（必読）

1. **「実装できる」と「儲かる」は別問題。** 本設計はシステムを動かすための設計であり、利益を保証するものではない。相場予測で継続的に利益を出すのは極めて難しい。
2. **必ずデモ口座（practice）から。** 実弁投入は長期のフォワードテスト後に限る。本番フラグは明示的・多重の安全装置の後ろに置く。
3. **バックテストの好成績を信用しすぎない。** 過学習・スリッページ・スプレッド拡大・約定遅延・指標時の急変で実運用は別物になる。
4. **自動売買は業者規約・法規制・税務の制約を受ける。** OANDAのAPI利用規約とレート制限を遵守する。

---

## 1. 全体アーキテクチャ

```
                         ┌─────────────────────────────────────────────┐
                         │                Web UI (Flask)                │
                         │  ダッシュボード / 取引ログ / 勝率 / 設定 / 緊急停止 │
                         └───────────────┬─────────────────────────────┘
                                         │ (既存のログイン認証を流用)
                                         │
┌──────────────┐   ┌───────────────────────────────────────────────────────┐
│ OANDA v20    │   │                   Trading Engine (Python)             │
│ - REST(価格)  │──▶│                                                       │
│ - Streaming  │   │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│ - 注文/口座   │◀──│  │ Data     │─▶│ Analysis │─▶│ Strategy │─▶│ Risk / │ │
└──────────────┘   │  │ Feed     │  │ (指標/AI) │  │ (シグナル)│  │ Order  │ │
                   │  └──────────┘  └──────────┘  └──────────┘  └────────┘ │
┌──────────────┐   │        │             │            │            │       │
│ 経済指標API   │──▶│        ▼             ▼            ▼            ▼       │
│ ニュースAPI   │   │  ┌──────────────────────────────────────────────────┐ │
└──────────────┘   │  │            Storage (PostgreSQL)                   │ │
                   │  │  candles / signals / trades / runs / metrics      │ │
┌──────────────┐   │  └──────────────────────────────────────────────────┘ │
│ Claude API   │──▶│        ▲                                               │
│ (LLM解析)     │   │        └──────── Learning / Stats (集計・ML) ─────────│
└──────────────┘   └───────────────────────────────────────────────────────┘
```

### レイヤーの責務

| レイヤー | 責務 | 主な入出力 |
|---|---|---|
| **Data Feed** | OANDAからローソク足/価格を取得・正規化・保存 | 入: OANDA API / 出: candles テーブル, メモリ上の最新足 |
| **Analysis** | テクニカル指標計算・レンジ/トレンド判定・ボラ分析・（発展）AI解析 | 入: candles / 出: 特徴量(features) |
| **Strategy** | 売買ルールの評価。条件成立でシグナル生成 | 入: features / 出: signal(BUY/SELL/NONE) |
| **Risk / Order** | ロット計算・許容リスク・指標前後の停止・発注/決済・トレーリング | 入: signal / 出: OANDA注文, trades テーブル |
| **Learning / Stats** | 取引ログ集計、通貨ペア・時間帯別勝率、（発展）ML学習 | 入: trades / 出: metrics, モデル |
| **Web UI** | 可視化・設定・手動オーバーライド・緊急停止 | 既存Flask資産を流用 |

---

## 2. 技術選定

| 項目 | 採用 | 理由 |
|---|---|---|
| 言語 | Python 3.11+ | 金融・ML・OANDAライブラリが充実 |
| 取引API | OANDA v20（`oandapyV20` または httpx直叩き） | デモ/本番が同一APIで切替容易、ストリーミング対応 |
| 指標計算 | `pandas` + `pandas-ta`（または `ta`） | ATR/ADX/EMA等を簡潔に。TA-Libはビルドが重いので回避 |
| バックテスト | 自前イベントドリブン + 検証に `backtesting.py` 併用 | ロジックを実エンジンと共有しやすい自前を主、ライブラリで相互検証 |
| DB | PostgreSQL（既存 docker-compose を流用）/ ローカルは SQLite | 既存資産との一貫性 |
| Web | Flask（既存）+ Chart.js / lightweight-charts | 認証・テンプレ資産を再利用 |
| スケジューラ | `APScheduler`（単機）→ 将来 Celery/常駐ループ | M15足クローズ毎の評価に十分 |
| LLM解析（発展） | **Claude API（`claude-opus-4-8` / コスト重視は `claude-haiku-4-5`）** | ニュース・中銀発言の解析、AI合議 |
| ML（発展） | `scikit-learn`（勾配ブースティング）→ 必要なら `lightgbm` | ダマシ予測など。まずは軽量モデル |
| 設定 | `.env` + `pydantic-settings` | 秘密情報の分離、型安全 |

> LLM/ML は **Phase 4以降**。Phase 1〜3 はルールベースのみで完結させる（決定論的でデバッグ容易）。

---

## 3. ストラテジー仕様（要求機能のロジック化）

### 3.1 相場分析（Analysis）

| 要求 | 実装方法 |
|---|---|
| レンジ/トレンド判定 | **ADX**: ADX≥25 かつ +DI/−DI の差でトレンド、ADX<20 でレンジ。補助に EMA(20/50/200) の並びと傾き |
| ボラティリティ分析 | **ATR(14)**。ATRのZスコア/百分位で「高ボラ/低ボラ」を区分。スプレッド比も監視 |
| 経済指標前後の危険度 | 経済指標カレンダー（例: ForexFactory/Investing系/有料API）を取り込み、対象通貨の**高重要度指標の前後N分はエントリー禁止 + 既存ポジは保護** |
| ダマシブレイク確率予測 | Phase1は近似ルール（ブレイク後の終値確定・リテスト・出来高/ATR増を要求）。Phase5でMLに置換 |

### 3.2 エントリー条件（Strategy）

「**M15でブレイク + 上位足の方向一致 + ATR/出来高確認**」を AND 条件で実装：

```
エントリー(BUY)成立条件:
  1. 環境認識: H1・H4・日足のトレンド方向がすべて「上」で一致
       - 各足のトレンド = EMA配列(短>中>長) かつ ADX≥閾値
  2. トリガー: M15 で直近Nバーの高値を「終値」でブレイク（ヒゲだけの抜けは除外）
  3. ボラ確認: ATR(M15) がしきい値以上（動意あり）/ スプレッドがATR比で許容内
  4. 出来高確認: tick volume が直近平均比で増加（OANDAは実出来高でなくtick volume）
  5. 危険度フィルタ: 対象通貨の高重要度指標が前後N分に無い
  → すべて満たせば BUY シグナル（SELL は上記を反転）
```

各条件はパラメータ化し、設定とバックテストで調整可能にする。

### 3.3 ポジション管理（Risk / Order）

| 要求 | 実装方法 |
|---|---|
| 利益を伸ばす（利確を遅らせる） | 含み益が `R`（初期リスク幅）の倍数に達したらトレーリング開始。トレンド継続（ADX維持）中はTPを固定せず伸ばす |
| 危険なら早期決済 | 上位足トレンドの崩れ・ボラ急変・指標接近・反対シグナルで部分/全決済 |
| 損切り位置の自動変更（トレーリング） | ATRトレーリング（`stop = price − k×ATR`）。建値到達でブレイクイーブンへ移動 |
| ロット/リスク管理 | 1トレードの損失を口座の `x%`（例 0.5〜1%）に固定。SL幅から逆算してロット決定。最大同時ポジション数・最大ドローダウンで停止 |

### 3.4 学習・統計（Learning / Stats）

| 要求 | 実装方法 |
|---|---|
| 勝因・敗因の蓄積 | 全トレードに**エントリー時の特徴量スナップショット**（ADX, ATR, MTF状態, 時間帯, スプレッド等）を保存し、結果(損益, R倍数)と紐付け |
| 通貨ペアごとの特徴学習 | ペア別に勝率・期待値・最適パラメータを集計。将来はペア別モデル |
| 時間帯ごとの勝率更新 | セッション（東京/ロンドン/NY）・曜日別に勝率/期待値を集計しダッシュボード表示。低勝率帯は自動的にフィルタ |

---

## 4. データ設計（主要テーブル）

```
candles        … 取得したローソク足
  id, instrument, granularity(M15/H1/H4/D), time, o,h,l,c, volume, complete

features       … 各足クローズ時に算出した指標スナップショット
  id, instrument, time, granularity, atr, adx, plus_di, minus_di,
  ema_fast, ema_mid, ema_slow, trend_state, regime(range/trend), vol_pct

signals        … 生成された売買シグナル
  id, instrument, time, side(BUY/SELL), reason(json: 成立条件), score

trades         … 実行したトレード（デモ/本番共通）
  id, instrument, side, entry_time, entry_price, sl, tp, units,
  exit_time, exit_price, pnl, r_multiple, status(open/closed),
  entry_features(json), exit_reason, environment(practice/live)

runs           … エンジンの起動セッション/バックテストラン
  id, mode(live/backtest), started_at, params(json), notes

metrics        … 集計済みKPI（ペア別/時間帯別/全体）
  id, scope(pair/session/overall), key, win_rate, expectancy, max_dd, updated_at
```

設計方針:
- **同一の `trades` スキーマをバックテストと実運用で共有** → 集計・可視化コードを1本化。
- `environment` 列で practice / live を厳密に分離。

---

## 5. 安全装置（最重要）

| 装置 | 内容 |
|---|---|
| 環境の既定値 | デフォルトは **practice**。live は環境変数 + UIの二重確認 + 明示フラグが揃って初めて有効 |
| キルスイッチ | Web UIとCLIの両方から**即時全決済 + 新規エントリー停止** |
| サーキットブレーカー | 日次/週次の最大損失・連敗数・最大DDを超えたら自動停止 |
| 冪等な発注 | client order id による重複発注防止。約定確認とDB記録を必ずペアで |
| レート制限遵守 | OANDAのAPIレート制限内でリトライ（指数バックオフ） |
| 監査ログ | 全注文・全決済・全停止イベントを追記専用ログに記録 |

---

## 6. 段階的ロードマップ

| Phase | 内容 | 成果物 | AI/LLM |
|---|---|---|---|
| **1. 基盤** | OANDAからローソク足取得・保存、指標計算（ATR/ADX/EMA/MTF判定）、イベントドリブンのバックテスト基盤 | `data feed`, `analysis`, `backtester`, candles/features テーブル | なし |
| **2. 自動売買（デモ）** | 3.2のエントリールール + 3.3のリスク/トレーリングを実装。**practice口座でペーパー/自動売買**。指標フィルタ | `strategy`, `risk/order`, trades 記録 | なし |
| **3. 可視化・統計** | 既存Flaskにダッシュボード追加（資産曲線・取引ログ・ペア別/時間帯別勝率・設定・キルスイッチ） | Web UI, metrics 集計 | なし |
| **4. LLM解析** | Claude APIでニュース・中銀発言を解析しスコア化、エントリー/サイズの補助フィルタに | news/sentiment モジュール | Claude API |
| **5. 高度化** | ダマシ予測ML（勾配ブースティング）、チャート画像認識、複数AIの合議評価 | ML pipeline, vision, multi-agent | Claude + ML |

各Phaseは**それ単体で価値があり、止めても次に進める**よう独立性を保つ。Phase 1〜3 を「動いて検証できる最小プロダクト」と位置づける。

---

## 7. ディレクトリ構成（実装時の想定）

```
ai-trade/
├── app.py                 # 既存: Flask認証（UIの土台に流用）
├── models.py              # 既存: User。trade系モデルを追加していく
├── docs/
│   └── DESIGN.md          # 本書
├── trading/               # ★新規: トレーディングエンジン
│   ├── config.py          # 設定（pydantic-settings, .env）
│   ├── oanda_client.py    # OANDA v20 ラッパ（practice/live切替）
│   ├── data_feed.py       # 足取得・保存
│   ├── indicators.py      # ATR/ADX/EMA 等
│   ├── analysis.py        # レンジ/トレンド・ボラ・MTF判定
│   ├── strategy.py        # エントリー条件
│   ├── risk.py            # ロット/リスク/トレーリング
│   ├── executor.py        # 発注・決済・約定確認
│   ├── backtester.py      # イベントドリブン検証
│   ├── stats.py           # 勝率・期待値の集計
│   └── engine.py          # ループ/スケジューラ統括
├── tests/                 # 各モジュールの単体・バックテスト回帰
└── requirements.txt       # oandapyV20, pandas, pandas-ta, apscheduler ...
```

---

## 8. 必要なもの / 環境変数

| 変数 | 用途 |
|---|---|
| `OANDA_API_TOKEN` | OANDA v20 アクセストークン |
| `OANDA_ACCOUNT_ID` | 口座ID |
| `OANDA_ENV` | `practice`（既定）/ `live` |
| `ANTHROPIC_API_KEY` | （Phase4以降）Claude API |
| `ECONOMIC_CALENDAR_API_KEY` | （任意）経済指標カレンダー |
| `DATABASE_URL` | 既存。Postgres接続 |
| `RISK_PER_TRADE` | 1トレードの口座リスク%（例 0.5） |
| `MAX_OPEN_POSITIONS` / `MAX_DAILY_LOSS` | サーキットブレーカー閾値 |

---

## 9. 未決事項（実装着手前に確定したい）

1. **対象通貨ペア**: まずは1〜2ペア（例: USD/JPY, EUR/USD）に絞るか。
2. **運用足**: トリガーをM15固定にするか、可変にするか。
3. **経済指標データ源**: 無料スクレイピング系か、有料API（安定性重視）か。
4. **稼働形態**: 常駐プロセス（VPS/コンテナ）か、cron的バッチか。OANDAは24時間なので常駐前提が自然。
5. **バックテスト用ヒストリカルデータ**: OANDAのcandles APIで足りるか（取得期間・粒度の上限を確認）。

---

## 10. 実装ステータス

- **Phase 1 実装済み**: `oanda_client` / `data_feed` / `indicators` / `analysis` / `strategy` / `backtester`（ルックアヘッド回避・R倍数評価）。テスト 8件パス。
- **Phase 2 実装済み**: `risk`（units逆算）/ `executor`（成行＋SL・ATRトレーリング・全決済）/ `safety`（サーキットブレーカー＋キルスイッチ）/ `engine`（1ティック処理＋常駐ループ）。テスト 14件パス。CLI: `scripts/run_engine.py`。
- **Phase 3 実装済み**: `store`（sqlite で取引永続化・R倍数算出）/ `metrics`（勝率・期待値・最大DD・資産曲線・セッション分類）/ `dashboard`（Flask Blueprint `/trading`：資産曲線・取引ログ・ペア別/セッション別勝率・キルスイッチ操作）。エンジンが約定/決済を store に自動記録。テスト 8件パス。
- **Phase 4 実装済み**: `news`（Claude `claude-opus-4-8` の構造化出力でニュース/中銀発言を解析→方向バイアス・リスク度・確信度をスコア化）。`sentiment_filter` がエントリー可否とロットサイズ係数を返し、engine が `news_provider` 経由で利用（高リスク/高確信逆風は見送り、弱い逆風は縮小）。テスト 10件パス。CLI: `scripts/analyze_news.py`。
- **次（Phase 5）**: ML（ダマシブレイク確率予測）・チャート画像認識（Claude vision）・複数AIエージェントの合議評価。
