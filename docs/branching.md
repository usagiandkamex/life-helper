# ブランチ戦略と開発の流れ

このリポジトリは **GitHub Flow** で運用します。`main` は常にデプロイ可能な状態に保ち、すべての変更は作業ブランチ → Pull Request → `main` の順で入れます。

## ブランチ

| ブランチ | 用途 | 寿命 |
|---|---|---|
| `main` | 本番。マージされると CI 合格後に Azure へ自動デプロイされる | 永続 |
| `feature/<内容>` | 機能追加（例: `feature/rakuten-travel-connector`） | PR をマージしたら削除 |
| `fix/<内容>` | 不具合修正 | 同上 |
| `chore/<内容>` | 依存関係・設定・CI などの保守 | 同上 |
| `docs/<内容>` | ドキュメントだけの変更 | 同上 |

- ブランチ名は英小文字とハイフン。1 つのブランチでは 1 つの目的だけを扱い、短期間でマージします。
- `main` から切り、`main` に戻します（`develop` などの長期ブランチは作りません）。

## main の運用ルール

`main` に GitHub のルールセットは設定しません。次のルールを運用で守ります。

- `main` へ直接 push または force push しない。
- 変更は作業ブランチから Pull Request を作り、CI の全ジョブが合格してからマージする。
- squash マージを使い、1 PR を `main` 上の 1 コミットにする。
- マージ後の作業ブランチは削除する。

## 1 回の変更の流れ

```powershell
git switch main
git pull
git switch -c feature/<内容>

# 変更して、ローカルで確認
cd app/backend; uv run ruff check src tests; uv run pytest -q; cd ../..
cd app/frontend; npm run lint; npm run build; cd ../..

git add -A
git commit -m "feat: <変更の要約>"
git push -u origin feature/<内容>
gh pr create --fill --base main          # PR テンプレートのチェックリストを埋める
gh pr checks --watch                     # CI が全部通ることを確認
gh pr merge --squash --delete-branch     # squash マージして作業ブランチを削除
```

マージされると、`main` の CI → `Deploy` ワークフローの順に実行され、CI が合格したコミットだけが Azure にデプロイされます。

## コミットメッセージと PR タイトル

squash マージでは **PR タイトルが `main` のコミットメッセージ**になります。[Conventional Commits](https://www.conventionalcommits.org/ja/) の形式にします。

| 接頭辞 | 用途 |
|---|---|
| `feat:` | 機能追加 |
| `fix:` | 不具合修正 |
| `docs:` | ドキュメント |
| `chore:` | 保守（依存関係・設定） |
| `ci:` | CI/CD |
| `refactor:` / `test:` | リファクタリング / テスト |

## 依存関係の更新

Dependabot（`.github/dependabot.yml`）が毎週、GitHub Actions と npm の更新 PR を作ります。CI が通れば通常の PR と同じようにマージします。
Python の依存関係は `pyproject.toml` の下限バージョンで管理しているため、CI・Docker ビルドのたびに最新の互換バージョンで検証されます。
