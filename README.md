# Life Helper

GitHub Copilot SDK を使った、**自分専用（GitHub アカウント `usagiandkamex`）のプライベート用サポートチャット**です。
ふるさと納税・ライフプラン・NISA などお金の相談、日々の調べもの、定期チェック（オートメーション）に使います。
会話・覚えたこと・ノートは Azure Files 上の Markdown に蓄積され、使うほど自分の状況を踏まえて答えられるようになります。

> お金に関する結果はすべて**目安**です。専門家（税理士・FP）の助言や投資助言ではありません。

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/setup.md](docs/setup.md) | **構築手順**（初回構築と CI/CD の設定。手元で 1 回だけ行う作業と、以降の自動デプロイを分けて説明） |
| [docs/branching.md](docs/branching.md) | ブランチ戦略と開発の流れ（作業ブランチ → PR → main） |
| [docs/development.md](docs/development.md) | ローカル開発 |
| [docs/operations.md](docs/operations.md) | 運用（知識ベース・税パラメータ・オートメーション・コネクタ・障害対応） |

## 主な機能

| 機能 | 内容 |
|---|---|
| チャット | Copilot SDK（`mode="empty"`）で回答。切断されても自動再接続して続きを受信（ACA の 240 秒制限対策） |
| 蓄積 | 会話は Copilot のセッション永続化で再開。覚えたことは `memories/`、ノートは `notes/`、資料は `docs/`（Markdown）。プロフィールは毎回反映 |
| 検索 | Copilot の組み込みツール（`grep` / `glob` / `view`）で知識ベースを検索（独自 DB・ベクトル検索なし） |
| お金の計算 | ふるさと納税の上限目安、ライフプラン、積立シミュレーション、譲渡益課税の目安（計算は決まった式で実行） |
| 資産 | 証券会社 CSV（SBI / 楽天）の取り込み、Stooq の前日終値で株価更新（無料）、投資信託は許可サイトか手入力 |
| オートメーション | チャットの指示を決めた周期で実行（ACA ジョブが 15 分ごとに確認、1 回 20 分まで）。結果はアプリ内に記録、GitHub Issue 通知は指定したものだけ |
| コネクタ | 楽天トラベル空室検索など。API キーは Azure のシークレットだけに保存し、モデル・定義・履歴には出さない |

## 構成

```
ブラウザ ── GitHub OAuth ──> Azure Container Apps（最小 0 / 最大 1）
                               └ FastAPI + Copilot SDK ── GitHub Copilot（usagiandkamex のサブスクリプション）
                               Azure Files (/data): Copilot のセッション状態、知識ベース（Markdown）、アプリの状態
ACA ジョブ（15 分ごと）── 期限が来たオートメーションを実行 ──> GitHub App で Issue 通知（指定したものだけ）
GitHub Actions ── PR で CI ──> main にマージ ──> CI 合格後に OIDC で Azure へ自動デプロイ
```

| ディレクトリ | 内容 |
|---|---|
| `app/backend` | Python 3.12 + FastAPI + `github-copilot-sdk` |
| `app/frontend` | React + Vite + TypeScript（ビルド成果物を FastAPI が配信） |
| `infra` | Bicep（Container Apps とスケジュールジョブ、Azure Files、ACR Basic、マネージド ID、Log Analytics、予算アラート） |
| `scripts` | CI/CD の設定 |
| `.github` | CI / Deploy ワークフロー、PR テンプレート、Dependabot |

使わないもの: Azure OpenAI、Cosmos DB、ベクトル検索、Key Vault、Entra ID によるアプリのログイン。

## 安全のしくみ（要点）

- ログインできるのは GitHub ユーザー ID `134019422`（`usagiandkamex`）だけ。GitHub トークンは Cookie に入れず、暗号化して Azure Files に保存（ジョブと共有）。
- Copilot の組み込みツールは `view` / `grep` / `glob` / `create` / `edit` / `web_fetch` / `skill` だけを許可（シェルは不可）。
  権限ハンドラと `pre_tool_use` フックの二重チェックで、読み取りは知識ベース内、書き込みは `memories/`・`notes/`・`plans/`・`INDEX.md` だけ、
  `web_fetch` は許可ドメイン（go.jp / lg.jp / 投資信託協会・運用会社）だけに制限。
- マイナンバー・口座番号・カード番号・パスワードはファイルに保存しない。
- API キー・トークンはツール結果・エラー・ログから自動でマスク。
- ログアウトするとサーバー側でセッションを失効（コピーされた Cookie も無効）。
- `main` への直接 push・force push は行わず、PR の CI 合格後に squash マージする。デプロイは OIDC で、Azure の資格情報を GitHub に保存しない。
