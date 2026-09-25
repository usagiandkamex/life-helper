# ローカル開発

## 起動

```powershell
# バックエンド（http://localhost:8000）
cd app/backend
uv sync
uv run playwright install --only-shell chromium   # ブラウザツール用（初回だけ。使わないなら $env:LH_BROWSER_ENABLED = "false"）
$env:LH_ENVIRONMENT = "development"
$env:LH_DATA_DIR = "../../.data"
$env:LH_STATIC_DIR = "../frontend/dist"
$env:LH_DEV_GITHUB_TOKEN = (gh auth token --user usagiandkamex)   # OAuth App なしでログインする
uv run uvicorn life_helper.main:create_app --factory --port 8000

# フロントエンド（別ターミナル。http://localhost:5173 から /api を 8000 に中継）
cd app/frontend
npm ci
npm run dev
```

ログイン画面の「開発用ログイン（gh auth token）」でログインします。開発用のキー類は `.data/app/secrets/` に自動生成されます（Git 管理外）。

Docker で動かす場合は、`.env.example` を `.env` にコピーして `docker compose up --build`。

## 確認コマンド

| 対象 | コマンド |
|---|---|
| バックエンドの lint | `cd app/backend; uv run ruff check src tests; uv run ruff format --check src tests` |
| バックエンドのテスト | `cd app/backend; uv run pytest -q` |
| フロントエンド | `cd app/frontend; npm run lint; npm run build` |
| Bicep | `az bicep build --file infra/main.bicep --stdout > $null` |
| オートメーションを 1 回実行 | `cd app/backend; uv run life-helper-job`（Docker なら `docker compose run --rm job`） |

PR を作ると同じ内容を CI が実行します（[branching.md](branching.md)）。

テストは名前解決を差し替えるため、外部には接続しません。実際の Chromium を使うテストは、Chromium が入っていないと自動でスキップします（CI では実行されません）。

## パッケージのミラーを使う場合

- `uv.lock` はコミットしません。CI と Docker ビルドで解決されます。
- ミラーは `pyproject.toml` に書かず、環境変数 `UV_DEFAULT_INDEX` で指定します。
- `npm install` でミラーを使った場合は、`package-lock.json` の `resolved` が `https://registry.npmjs.org/` になっていることを確認してから PR を作ります。
