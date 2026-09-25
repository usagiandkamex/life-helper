# 構築手順

Life Helper を Azure に構築し、以降は Pull Request をマージするだけで自動デプロイされる状態にするまでの手順です。

## 全体像

| ステップ | 内容 | 作業場所 | 実行者 | 回数 |
|---|---|---|---|---|
| 0 | 手元のツールを準備 | 手元の PC | 自分 | 1 回だけ |
| 1 | GitHub の準備（Copilot・通知用 App など） | ブラウザ + 手元 | 自分 | 1 回だけ |
| 2 | Azure の初回構築（リソース作成 → OAuth App → デプロイ） | 手元の PC（azd） | 自分 | 1 回だけ |
| 3 | CI/CD の設定（Azure と GitHub の連携） | 手元の PC（スクリプト） | 自分 | 1 回だけ |
| 4 | 以降の変更（ブランチ → PR → マージ → 自動デプロイ） | GitHub | CI/CD が自動 | 変更のたび |

- **ステップ 0〜3 は最初に 1 回だけ**、手元から行います。Azure のリソースは手元の `azd` で作ります。
- **ステップ 4 以降は CI/CD が自動で行います**。手元から `azd` を実行する必要はありません。
- 設定値（シークレットなど）の正本は **手元の azd 環境**です。ステップ 3 のスクリプトがそれを GitHub にコピーします（[設定値の変更](#設定値を変更するとき)）。

> 費用: Azure は ACR Basic とストレージが中心で月数百〜千円程度です。そのほかの有料サービス（有料の市場データ API、Azure Backup など）は使いません。追加するときは事前に判断してください。

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

### 1-1. Copilot の設定を確認

<https://github.com/settings/copilot> で、Copilot のプラン（Pro 以上を推奨）と、プロンプトの学習利用などの設定を確認します。

### 1-2. （任意）通知用の GitHub App

オートメーションの結果を GitHub Issue で受け取る場合だけ必要です。自分のトークンで Issue を作ると自分の操作になって通知が届かないため、GitHub App を使います。

1. `usagiandkamex` でプライベートリポジトリ `life-helper-notifications` を作る。
2. <https://github.com/settings/apps> → **New GitHub App**。Webhook はオフ、**Repository permissions → Issues: Read and write** だけを付ける。
3. 作成後の **General** 画面に表示される数値の **App ID** を控える。2-2のOAuth AppのClient IDとは別の値。
4. **Private keys** → **Generate a private key** を押し、ダウンロードされた `.pem` ファイルを安全な場所に保存する。これが `LH_GITHUB_APP_PRIVATE_KEY` の取得元。
5. 左メニューの **Install App** → `usagiandkamex` の **Install** → **Only select repositories** で `life-helper-notifications` だけを選び、インストールする。
6. インストール後に開く `https://github.com/settings/installations/<数値>` の末尾の数値を控える。これが `LH_GITHUB_APP_INSTALLATION_ID`。
7. GitHubモバイルアプリで通知を受け取れるようにし、`life-helper-notifications` を **Watch → All Activity** にする。

### 1-3. （任意・無料）外部サービスの API キー

| サービス | 用途 | 取得 |
|---|---|---|
| 楽天ウェブサービス | 楽天市場・楽天トラベル・楽天ブックス・楽天Kobo・楽天GORA・楽天レシピの検索（コネクタ「楽天ウェブサービス」） | アプリを 1 つ登録し、`applicationId` と `accessKey` を取得。登録にアプリの URL が必要なので、[2-3](#楽天ウェブサービスのアプリを登録する) で行う |

株価（日本株・米国株・ETF・REIT の前日終値）と USD/JPY は、Yahoo Finance のチャート API から取得します。API キーも設定も不要です。
公式に公開された API ではない（yfinance などが使っているものと同じ）ため、個人利用の範囲で使い、仕様変更や利用制限で取得できないときは証券会社の保有証券 CSV の取り込みで補ってください。

投資信託の基準価額の取得元は、いずれも API キー不要で設定も要りません（各サイトの利用規約の範囲内で個人利用）。
主な取得元は[投資信託協会 投信総合検索ライブラリー](https://toushin-lib.fwg.ne.jp/)で、すべての運用会社のファンドに対応します。
[楽天投信投資顧問](https://www.rakuten-toushin.co.jp/fund/nav/)・[大和アセットマネジメント](https://www.daiwa-am.co.jp/funds/)の公式 CSV は予備です。
ポートフォリオ画面の「評価額を計算」で、取得元が未設定のファンドはファンド名から自動で紐付けます。紐付けられなかったファンドは「取得元を設定」で候補を選ぶか、
コードを指定します（コードの調べ方は [docs/operations.md](operations.md) 参照）。どれにも当てはまらないファンドは「手入力」を選び、公式サイトの基準価額を入力します。

## ステップ 2: Azure の初回構築（1 回だけ・手元から）

### 2-1. azd 環境を作り、Azure のリソースを作成する

```powershell
# 利用可能なサブスクリプションを確認し、個人用の Subscription ID を控える
az account list --query "[].{Name:name, SubscriptionId:id, TenantId:tenantId, Default:isDefault}" -o table

azd env new life-helper                   # 環境名（リソース名に使われる）
azd env set AZURE_SUBSCRIPTION_ID <個人用の Subscription ID>
azd env set AZURE_LOCATION japaneast

# 選択したサブスクリプションを確認
azd env get-value AZURE_SUBSCRIPTION_ID
az account show --subscription (azd env get-value AZURE_SUBSCRIPTION_ID) --query "{Name:name, SubscriptionId:id, TenantId:tenantId}" -o table

# アプリの動作に必須のシークレット（ランダムに生成）
azd env set LH_SESSION_SECRET (python -c "import secrets; print(secrets.token_urlsafe(48))")
azd env set LH_TOKEN_ENCRYPTION_KEY (python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

azd provision
```

`AZURE_SUBSCRIPTION_ID` はこの `azd` 環境に保存され、以降のプロビジョニング、デプロイ、CI/CD 設定で使用されます。
`az account` の既定サブスクリプションだけに依存せず、必ず個人用サブスクリプションを明示してください。

リソースグループ `rg-life-helper` に、Container Apps（アプリ + オートメーション用ジョブ）、Azure Files、ACR、Log Analytics などが作られます。
この時点のアプリは仮のコンテナです。出力された **`SERVICE_APP_ENDPOINT_URL`**（`https://ca-lifehelper-xxxx....azurecontainerapps.io`）を控えます。

```powershell
azd env get-value SERVICE_APP_ENDPOINT_URL   # あとから確認する場合
```

### 2-2. GitHub OAuth App を作り、azd 環境に登録する

<https://github.com/settings/developers> → **OAuth Apps** → **New OAuth App**（`usagiandkamex` で作成）

まず、入力に使う URL を確認します。

```powershell
$baseUrl = (azd env get-value SERVICE_APP_ENDPOINT_URL).TrimEnd('/')
$baseUrl
"$baseUrl/auth/callback"
```

| 項目 | 値 |
|---|---|
| Application name | `Life Helper` |
| Homepage URL | `$baseUrl`（2-1 の `SERVICE_APP_ENDPOINT_URL`） |
| Application description | 空欄でよい |
| Redirect URI | `$baseUrl/auth/callback` |
| Allow wildcard matching | オフ |
| Enable Device Flow | オフ |
| Expire user access tokens | オフ |

`Expire user access tokens` は必ずオフにしてください。このアプリは refresh token の更新を行わないため、オンにすると期限切れ後にGitHub連携が動かなくなります。

**Register application** を押した後、表示された **Client ID** を控えます。続いて **Generate a new client secret** を押し、表示された Client secret をその場で控えます。Client secretは再表示できません。

```powershell
azd env set LH_GITHUB_OAUTH_CLIENT_ID <Client ID>
azd env set LH_GITHUB_OAUTH_CLIENT_SECRET <Client secret>
```

Client secret はリポジトリのファイルに保存しないでください。

### 2-3. （任意）オプションの設定を登録する

使うものだけ登録します。あとから追加する場合も同じコマンドです（[設定値を変更するとき](#設定値を変更するとき)）。

#### 楽天ウェブサービスのアプリを登録する

楽天の検索（無料）を使う場合だけ必要です。アプリを 1 つ登録し、市場・トラベル・ブックス・Kobo・GORA・レシピの 6 つの API をすべて許可します。
キーは 6 つの API で共通で、アプリではコネクタ「楽天ウェブサービス」として扱います。

1. <https://webservice.rakuten.co.jp/> に楽天会員でログインし、新しいアプリの登録画面を開く。
2. 次のとおり入力する（項目名は画面の表記に合わせて読み替えてください）。

   | 項目 | 値 |
   |---|---|
   | Application Name | `Life Helper` |
   | Application URL | `$baseUrl`（2-1 の `SERVICE_APP_ENDPOINT_URL`） |
   | Application Type | Web アプリケーション |
   | Allowed websites（許可された Web サイト） | `$baseUrl`。ドメインだけを入れる欄なら `https://` を除いたホスト名 |
   | Purpose of data usage（データの利用目的） | 個人用の生活支援アプリで、商品・宿・本・電子書籍・ゴルフ場・レシピを検索するため |
   | Expected QPS（想定 QPS） | `1` |
   | API Access Scopes | **6 つすべて**にチェック: Rakuten Ichiba API / Rakuten Travel API / Rakuten Books API / Rakuten Kobo API / Rakuten GORA API / Rakuten Recipe API |

3. CAPTCHA を入力して登録し、表示された **アプリ ID**（`applicationId`）と **アクセスキー**（`accessKey`、`pk_` で始まる）を控え、下の `azd env set` で登録する。

- アプリは楽天の API を呼ぶとき、`Referer` と `Origin` にアプリの URL（`LH_BASE_URL`）を付けます。
  「許可された Web サイト」とアプリの URL が一致しないと、楽天のツールが 403 エラー（アプリ ID・アクセスキーと許可された Web サイトの確認を促すメッセージ）を返します。
- 楽天の利用上限はアプリ ID ごとに 1 秒 1 回程度です。アプリは 6 つの API すべてを、Web アプリとオートメーションのジョブをまたいで 1.5 秒間隔に揃えます。
- アクセスキーは Client secret と同じ秘密情報です。リポジトリのファイルやチャットに書かないでください。

```powershell
# 外部サービス（ステップ 1-3・楽天ウェブサービスのアプリ）
azd env set LH_RAKUTEN_APPLICATION_ID <applicationId>
azd env set LH_RAKUTEN_ACCESS_KEY <accessKey>
```

#### そのほかのオプション

```powershell
# GitHub 通知（任意、ステップ 1-2で作成したGitHub Appの値）
azd env set LH_GITHUB_APP_ID <App ID>
$privateKey = (Get-Content -LiteralPath 'C:\秘密鍵を保存した場所\app-name.private-key.pem') -join '\n'
azd env set LH_GITHUB_APP_PRIVATE_KEY -- $privateKey
Remove-Variable privateKey
azd env set LH_GITHUB_APP_INSTALLATION_ID <installation ID>
azd env set LH_NOTIFY_REPO usagiandkamex/life-helper-notifications

# 予算アラート（請求通貨の月額）。開始日は今月 1 日にして、以後は変えない（Azure は予算の開始日を変更できない）
azd env set LH_BUDGET_AMOUNT 1500
azd env set LH_BUDGET_EMAIL <通知先メールアドレス>
azd env set LH_BUDGET_START_DATE 2026-10-01
```

2-2で作成したOAuth Appからは、GitHub Appの秘密鍵やInstallation IDは取得できません。通知を使わない場合は、上記の `LH_GITHUB_APP_*` と `LH_NOTIFY_REPO` の4行をすべて省略できます。
`-join '\n'` はPEMの実改行を文字列の `\n` に変換し、BicepパラメータのJSONを壊さずに渡せるようにします。アプリは起動時に元の改行へ戻します。
`--` は、PEMの先頭にある `-----` を `azd` がフラグと誤認するのを防ぎます。
秘密鍵の内容を画面やログへ表示せず、`.pem` ファイルはリポジトリへコピーまたはコミットしないでください。誤って画面、ログ、チャットなどへ秘密鍵本体を出した場合は、その鍵をGitHub Appの **Private keys** から削除し、新しい鍵を生成して登録し直してください。

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

### 3-1. CI/CD設定を作成

まず自動デプロイを有効にせず、AzureとGitHubの連携設定だけを作成します。

```powershell
./scripts/setup-cicd.ps1
```

このスクリプトが行うこと:

| 処理 | 内容 |
|---|---|
| Azure の ID を作成 | アプリ登録 `life-helper-github-deploy` に、GitHub APIから取得したリポジトリ固有のOIDC Subjectでフェデレーション資格情報を付ける。パスワードやキーは作らない |
| 権限を付与 | サブスクリプションに「共同作成者」、`rg-life-helper` に「ユーザー アクセス管理者」（ACR からの取得権限の割り当てに必要） |
| GitHub に登録 | 環境 `production` を作成し、azd 環境の値を変数（ID・URL など）とシークレット（キー類）として登録 |
| 自動デプロイを有効化 | リポジトリ変数 `DEPLOY_ENABLED=true`（`-EnableDeploy` を付けた場合） |

エラーなく完了したら、登録内容を確認します。シークレットの値自体は表示されません。

```powershell
gh variable list --env production --repo usagiandkamex/life-helper
gh secret list --env production --repo usagiandkamex/life-helper
```

### 3-2. 自動デプロイを有効化して確認

設定内容に問題がなければ、同じスクリプトを再実行して自動デプロイを有効化します。スクリプトは再実行しても同じ結果になります。

```powershell
./scripts/setup-cicd.ps1 -EnableDeploy
gh variable get DEPLOY_ENABLED --repo usagiandkamex/life-helper
```

`true` と表示されることを確認し、GitHubの **Actions → Deploy → Run workflow** で手動実行します。Deployが成功し、アプリへ再度GitHubログインできることを確認してください。

この後は、`main`のCIが成功するたびにDeployが自動実行されます。

## ステップ 4: 以降の変更（自動）

```
作業ブランチ ──push──> Pull Request ──CI（backend / frontend / infra / docker / secret-scan）
      │                                   │ 全部合格
      │                                   ▼
      └──────────── squash マージ ──> main ──CI──> 合格したら Deploy ──> Azure に反映
```

1. `feature/…` などのブランチを切って変更し、PR を作る（手順は [branching.md](branching.md)）。
2. CI が全部合格したことを確認し、squash マージする。`main` へ直接 push または force push はしない。
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
| `LH_RAKUTEN_APPLICATION_ID` / `LH_RAKUTEN_ACCESS_KEY` | | シークレット | 楽天ウェブサービス（市場・トラベル・ブックス・Kobo・GORA・レシピの検索） | 2-3 |
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
- 以前 Stooq 用に登録した `LH_STOOQ_API_KEY` は使われなくなりました（株価は Yahoo Finance から API キーなしで取得します）。
  残っていても害はありませんが、`gh secret delete LH_STOOQ_API_KEY --env production --repo usagiandkamex/life-helper` で削除できます。

## 困ったとき

| 症状 | 確認すること |
|---|---|
| `azd provision` が `invalid character '\n' in string literal` で失敗する | `LH_GITHUB_APP_PRIVATE_KEY` をステップ2-3の `-join '\n'` を使ったコマンドで上書きしてから再実行する |
| DeployのAzureログインが`AADSTS700213: No matching federated identity record`で失敗する | `./scripts/setup-cicd.ps1 -EnableDeploy`を再実行し、GitHubのimmutable OIDC Subjectにフェデレーション資格情報を更新してからDeployを再実行する |
| DeployのProvisionがContainer Appの`Circular dependency detected`で失敗する | 修正版のBicepとDeployワークフローを`main`へマージし、古い実行の再実行ではなく、新しいDeployを手動実行する |
| ログイン画面に「OAuth App が未設定」と出る | ステップ 2-2 の登録後に `azd provision` を実行したか |
| ログイン後に「再ログインが必要」と出る | GitHub 側でトークンを取り消していないか。ログインし直す |
| 株価の更新で「Yahoo Finance の利用制限」「想定外の応答」と出る | 非公式 API のため、時間をおいて再実行する。続く場合は仕様変更の可能性があるので、その間は証券会社の保有証券 CSV を取り込んで評価額を更新する |
| 投資信託が「自動では紐付けられなかった」と出る | 理由（同じ名前のファンドが複数・見つからない・基準価額が大きく違う）を確認し、「候補を見る」から公式ファンドを選ぶ。候補にない場合は ISIN を指定するか、手入力にする |
| 投資信託の基準価額の取得で、投資信託協会のエラーが出る | 公開サイトのため、時間をおいて「評価額を計算」をやり直す。続く場合は仕様変更の可能性があるので、楽天・大和の公式 CSV に紐付け直すか、公式サイトの基準価額を手入力する |
| 楽天のツールが 403（`Invalid Access Key` など）を返す | `LH_RAKUTEN_APPLICATION_ID` / `LH_RAKUTEN_ACCESS_KEY` が正しいか。楽天のアプリの「許可された Web サイト」がアプリの URL（`SERVICE_APP_ENDPOINT_URL`）と一致しているか。使う API の API Access Scopes が許可されているか |
| 楽天のツールが「利用回数の上限」「混み合っています」と返す | 楽天の利用上限（アプリ ID ごとに 1 秒 1 回程度）に達している。時間をおいてやり直す。オートメーションの周期を短くしすぎていないか |
| Deploy ワークフローが実行されない | リポジトリ変数 `DEPLOY_ENABLED` が `true` か。`main` の CI が合格しているか |
| Deploy がサインインで失敗する | ステップ 3 を実行したか。リポジトリ名を変えた場合はフェデレーション資格情報のサブジェクトも変わるため、スクリプトを再実行する |
| オートメーションが動かない | 画面の実行履歴、Azure Portal のジョブ `caj-lifehelper-…` の実行履歴とログ |
| アプリのログを見たい | Azure Portal → Log Analytics（`log-…`）→ `ContainerAppConsoleLogs_CL` |

## 削除する

```powershell
azd down --purge    # Azure のリソースをすべて削除（Azure Files の知識ベースも消えるため、先に画面から ZIP で書き出す）
```

GitHub 側は、アプリ登録 `life-helper-github-deploy`（Azure Portal の Entra ID）、OAuth App、GitHub App を必要に応じて削除します。
