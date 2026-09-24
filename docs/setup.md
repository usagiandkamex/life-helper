# 構築手順

Life Helper を Azure に構築し、以降は Pull Request をマージするだけで自動デプロイされる状態にするまでの手順です。

## 全体像

| ステップ | 内容 | 作業場所 | 実行者 | 回数 |
|---|---|---|---|---|
| 0 | 手元のツールを準備 | 手元の PC | 自分 | 1 回だけ |
| 1 | GitHub の準備（Pro・リポジトリ設定・通知用 App など） | ブラウザ + 手元 | 自分 | 1 回だけ |
| 2 | Azure の初回構築（リソース作成 → OAuth App → デプロイ） | 手元の PC（azd） | 自分 | 1 回だけ |
| 3 | CI/CD の設定（Azure と GitHub の連携） | 手元の PC（スクリプト） | 自分 | 1 回だけ |
| 4 | 以降の変更（ブランチ → PR → マージ → 自動デプロイ） | GitHub | CI/CD が自動 | 変更のたび |

- **ステップ 0〜3 は最初に 1 回だけ**、手元から行います。Azure のリソースは手元の `azd` で作ります。
- **ステップ 4 以降は CI/CD が自動で行います**。手元から `azd` を実行する必要はありません。
- 設定値（シークレットなど）の正本は **手元の azd 環境**です。ステップ 3 のスクリプトがそれを GitHub にコピーします（[設定値の変更](#設定値を変更するとき)）。

> 費用: Azure は ACR Basic とストレージが中心で月数百〜千円程度、GitHub Pro は月 4 ドル程度です。そのほかの有料サービス（有料の市場データ API、Azure Backup など）は使いません。追加するときは事前に判断してください。

---

## ステップ 0: 手元のツールを準備（1 回だけ）

| ツール | 用途 | 確認 |
|---|---|---|
| Git / GitHub CLI（`gh`） | リポジトリ操作 | `gh auth status` で `usagiandkamex` がログイン中 |
| Azure CLI（`az`）+ containerapp 拡張 | Azure 操作 | `az extension add --name containerapp --upgrade --yes` |
| Azure Developer CLI（`azd`） | 構築・デプロイ | `azd version` |
| PowerShell 7（`pwsh`） | 設定スクリプト | `pwsh --version` |
| Python 3.12 / uv、Node.js 24 | ローカル開発（任意） | `uv --version`、`node --version` |

```powershell
gh auth login          # usagiandkamex でログイン
az login               # 個人の Azure サブスクリプション（会社のものは使わない）
azd auth login
git clone https://github.com/usagiandkamex/life-helper.git
cd life-helper
```

## ステップ 1: GitHub の準備（1 回だけ）

### 1-1. GitHub Pro にアップグレード

<https://github.com/settings/billing> で `usagiandkamex` を GitHub Pro にします。
プライベートリポジトリで main の保護（ルールセット）とデプロイ用の環境を使うために必要です。

### 1-2. Copilot の設定を確認

<https://github.com/settings/copilot> で、Copilot のプラン（Pro 以上を推奨）と、プロンプトの学習利用などの設定を確認します。

### 1-3. リポジトリの設定（main の保護・デプロイ用の環境）

```powershell
./scripts/setup-github-repo.ps1
```

次を設定します（詳細は [branching.md](branching.md)）。
- マージは squash のみ、マージ後に作業ブランチを自動削除、auto-merge を有効化
- ルールセット `protect-main`: PR 必須・CI の全ジョブ合格必須・直接 push と強制 push の禁止
- デプロイ用の環境 `production`（`main` からのみデプロイ可能）

### 1-4. （任意）通知用の GitHub App

オートメーションの結果を GitHub Issue で受け取る場合だけ必要です。自分のトークンで Issue を作ると自分の操作になって通知が届かないため、GitHub App を使います。

1. `usagiandkamex` でプライベートリポジトリ `life-helper-notifications` を作る。
2. <https://github.com/settings/apps> → **New GitHub App**。Webhook はオフ、**Repository permissions → Issues: Read and write** だけを付ける。
3. **Generate a private key** で `.pem` を保存し、App ID を控える。
4. **Install App** → 通知用リポジトリだけにインストールし、URL 末尾の数字（installation ID）を控える。
5. GitHub モバイルアプリで通知を受け取れるようにしておく。

### 1-5. （任意・無料）外部サービスの API キー

| サービス | 用途 | 取得 |
|---|---|---|
| Stooq | 国内株・ETF の前日終値 | ブラウザで CAPTCHA を解いて API キーを取得 |
| 楽天ウェブサービス | 楽天トラベルの空室検索 | アプリ登録で `applicationId` と `accessKey` を取得 |

## ステップ 2: Azure の初回構築（1 回だけ・手元から）

### 2-1. azd 環境を作り、Azure のリソースを作成する

```powershell
azd env new life-helper                   # 環境名（リソース名に使われる）
azd env set AZURE_LOCATION japaneast

# アプリの動作に必須のシークレット（ランダムに生成）
azd env set LH_SESSION_SECRET (python -c "import secrets; print(secrets.token_urlsafe(48))")
azd env set LH_TOKEN_ENCRYPTION_KEY (python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

azd provision
```

リソースグループ `rg-life-helper` に、Container Apps（アプリ + オートメーション用ジョブ）、Azure Files、ACR、Log Analytics などが作られます。
この時点のアプリは仮のコンテナです。出力された **`SERVICE_APP_ENDPOINT_URL`**（`https://ca-lifehelper-xxxx....azurecontainerapps.io`）を控えます。

```powershell
azd env get-value SERVICE_APP_ENDPOINT_URL   # あとから確認する場合
```

### 2-2. GitHub OAuth App を作り、azd 環境に登録する

<https://github.com/settings/developers> → **OAuth Apps** → **New OAuth App**（`usagiandkamex` で作成）

| 項目 | 値 |
|---|---|
| Application name | `Life Helper` |
| Homepage URL | 2-1 の `SERVICE_APP_ENDPOINT_URL` |
| Authorization callback URL | `SERVICE_APP_ENDPOINT_URL` + `/auth/callback` |

**Generate a new client secret** で Client secret を作り、Client ID と一緒に登録します。

```powershell
azd env set LH_GITHUB_OAUTH_CLIENT_ID <Client ID>
azd env set LH_GITHUB_OAUTH_CLIENT_SECRET <Client secret>
```

### 2-3. （任意）オプションの設定を登録する

使うものだけ登録します。あとから追加する場合も同じコマンドです（[設定値を変更するとき](#設定値を変更するとき)）。

```powershell
# 外部サービス（ステップ 1-5）
azd env set LH_STOOQ_API_KEY <Stooq の API キー>
azd env set LH_RAKUTEN_APPLICATION_ID <applicationId>
azd env set LH_RAKUTEN_ACCESS_KEY <accessKey>

# GitHub 通知（ステップ 1-4）
azd env set LH_GITHUB_APP_ID <App ID>
azd env set LH_GITHUB_APP_PRIVATE_KEY (Get-Content .\life-helper.private-key.pem -Raw)
azd env set LH_GITHUB_APP_INSTALLATION_ID <installation ID>
azd env set LH_NOTIFY_REPO usagiandkamex/life-helper-notifications

# 予算アラート（請求通貨の月額）。開始日は今月 1 日にして、以後は変えない（Azure は予算の開始日を変更できない）
azd env set LH_BUDGET_AMOUNT 1500
azd env set LH_BUDGET_EMAIL <通知先メールアドレス>
azd env set LH_BUDGET_START_DATE 2026-10-01
```

### 2-4. アプリをデプロイし、本番設定を適用する

```powershell
azd deploy      # コンテナイメージを ACR でビルドしてアプリに適用（ジョブのイメージも自動で更新）
azd provision   # 本番設定（ポート 8000・/data のマウント・2-2 と 2-3 の設定）を適用
```

`azd deploy` はイメージだけを差し替えるため、初回だけ `azd provision` をもう一度実行して、仮のコンテナ用の設定から本番用の設定に切り替えます。

### 2-5. 動作を確認する

1. `SERVICE_APP_ENDPOINT_URL` を開き、**GitHub でログイン** → `usagiandkamex` でログインする。
2. チャットで質問して回答が返ることを確認する（初回は起動に数十秒かかることがあります）。
3. 設定画面で、外部サービス（コネクタ）と GitHub 通知の登録状況を確認する。

## ステップ 3: CI/CD の設定（1 回だけ・手元から）

```powershell
./scripts/setup-cicd.ps1 -EnableDeploy
```

このスクリプトが行うこと:

| 処理 | 内容 |
|---|---|
| Azure の ID を作成 | アプリ登録 `life-helper-github-deploy` に OIDC のフェデレーション資格情報（`repo:usagiandkamex/life-helper:environment:production`）を付ける。パスワードやキーは作らない |
| 権限を付与 | サブスクリプションに「共同作成者」、`rg-life-helper` に「ユーザー アクセス管理者」（ACR からの取得権限の割り当てに必要） |
| GitHub に登録 | 環境 `production` に、azd 環境の値を変数（ID・URL など）とシークレット（キー類）として登録 |
| 自動デプロイを有効化 | リポジトリ変数 `DEPLOY_ENABLED=true`（`-EnableDeploy` を付けた場合） |

確認: GitHub の **Actions → Deploy → Run workflow** で手動実行し、成功することを確かめます。

## ステップ 4: 以降の変更（自動）

```
作業ブランチ ──push──> Pull Request ──CI（backend / frontend / infra / docker / secret-scan）
      │                                   │ 全部合格
      │                                   ▼
      └──────────── squash マージ ──> main ──CI──> 合格したら Deploy ──> Azure に反映
```

1. `feature/…` などのブランチを切って変更し、PR を作る（手順は [branching.md](branching.md)）。
2. CI が全部合格したら squash マージ（`gh pr merge --squash --auto` なら自動）。
3. `main` の CI が合格すると、Deploy ワークフローが OIDC で Azure にサインインし、`azd provision` → `azd deploy` → `azd provision` を実行する。

---

## 設定値の一覧

| 名前 | 必須 | 種類 | 用途 | 登録するステップ |
|---|---|---|---|---|
| `AZURE_LOCATION` | ○ | 変数 | Azure のリージョン | 2-1 |
| `LH_SESSION_SECRET` | ○ | シークレット | ログイン Cookie の署名 | 2-1 |
| `LH_TOKEN_ENCRYPTION_KEY` | ○ | シークレット | 保存する GitHub トークンの暗号化 | 2-1 |
| `LH_GITHUB_OAUTH_CLIENT_ID` | ○ | 変数 | GitHub ログイン | 2-2 |
| `LH_GITHUB_OAUTH_CLIENT_SECRET` | ○ | シークレット | GitHub ログイン | 2-2 |
| `LH_STOOQ_API_KEY` | | シークレット | 株価（Stooq） | 2-3 |
| `LH_RAKUTEN_APPLICATION_ID` / `LH_RAKUTEN_ACCESS_KEY` | | シークレット | 楽天トラベル空室検索 | 2-3 |
| `LH_GITHUB_APP_ID` / `LH_GITHUB_APP_INSTALLATION_ID` | | 変数 | GitHub 通知 | 2-3 |
| `LH_GITHUB_APP_PRIVATE_KEY` | | シークレット | GitHub 通知 | 2-3 |
| `LH_NOTIFY_REPO` | | 変数 | 通知を作るリポジトリ | 2-3 |
| `LH_BUDGET_AMOUNT` / `LH_BUDGET_EMAIL` / `LH_BUDGET_START_DATE` | | 変数 | 予算アラート | 2-3 |
| `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` / `AZURE_ENV_NAME` | ○ | 変数（GitHub） | CI/CD の Azure サインイン | 3（スクリプトが登録） |
| `DEPLOY_ENABLED` | ○ | リポジトリ変数（GitHub） | 自動デプロイの有効化 | 3（スクリプトが登録） |

保存場所: 手元の azd 環境（`.azure/<環境名>/.env`、Git 管理外）→ Azure では Container Apps のシークレット、GitHub では環境 `production` の変数・シークレット。
リポジトリのファイルには一切書きません。

## 設定値を変更するとき

例: 楽天のキーを追加する、シークレットを更新する。

```powershell
azd env set LH_RAKUTEN_ACCESS_KEY <新しい値>   # 1. 手元の azd 環境（正本）を更新
./scripts/setup-cicd.ps1                         # 2. GitHub の環境 production に反映
gh workflow run Deploy                           # 3. デプロイして Azure に反映（または手元で azd provision）
```

- `LH_TOKEN_ENCRYPTION_KEY` を変えると保存済みの GitHub トークンを復号できなくなります。変更後にもう一度ログインしてください。
- `LH_SESSION_SECRET` を変えると全ブラウザがログアウトされます。
- `LH_BUDGET_START_DATE` は変更しないでください（予算を作り直す場合は Azure Portal で予算を削除してから）。

## 困ったとき

| 症状 | 確認すること |
|---|---|
| ログイン画面に「OAuth App が未設定」と出る | ステップ 2-2 の登録後に `azd provision` を実行したか |
| ログイン後に「再ログインが必要」と出る | GitHub 側でトークンを取り消していないか。ログインし直す |
| Deploy ワークフローが実行されない | リポジトリ変数 `DEPLOY_ENABLED` が `true` か。`main` の CI が合格しているか |
| Deploy がサインインで失敗する | ステップ 3 を実行したか。リポジトリ名を変えた場合はフェデレーション資格情報のサブジェクトも変わるため、スクリプトを再実行する |
| オートメーションが動かない | 画面の実行履歴、Azure Portal のジョブ `caj-lifehelper-…` の実行履歴とログ |
| アプリのログを見たい | Azure Portal → Log Analytics（`log-…`）→ `ContainerAppConsoleLogs_CL` |

## 削除する

```powershell
azd down --purge    # Azure のリソースをすべて削除（Azure Files の知識ベースも消えるため、先に画面から ZIP で書き出す）
```

GitHub 側は、アプリ登録 `life-helper-github-deploy`（Azure Portal の Entra ID）、OAuth App、GitHub App を必要に応じて削除します。
