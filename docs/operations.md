# 運用

## 知識ベース（Azure Files の `/data/knowledge`）

| フォルダ | 内容 | 編集 |
|---|---|---|
| `profile/` | 自分の基本情報・回答の好み（毎回の会話に反映） | 画面から本人だけ |
| `memories/` | 会話から覚えたこと | Copilot・画面 |
| `notes/`・`plans/` | ノート、ライフプランの記録 | Copilot・画面 |
| `docs/` | アップロード資料（Markdown に変換。原本は保存しない。スキャン PDF は非対応） | 画面 |
| `money/portfolio.yaml` | 保有銘柄 | 資産画面・ツール |

- 画面「知識・メモリ」の **ZIP で書き出し** でいつでもバックアップできます。
- Azure Files の共有は、削除しても 14 日間は復元できます（論理削除）。定期スナップショットが必要なら Azure Backup（有料）を検討します（使う前に費用を判断）。
- マイナンバー・口座番号・カード番号・パスワードはファイルに保存されません（Copilot の書き込みは拒否、アップロードは伏せ字で保存）。

## 年度ごとの税パラメータ

`app/backend/src/life_helper/resources/tax_params/<年>.yaml` を毎年更新します（ブランチ → PR で変更）。
出典（国税庁・総務省）で値を確認して `status: verified` にします。`provisional` の間、および未登録の年度で計算すると、結果に警告が表示されます。

## 証券会社 CSV の列

`resources/broker_csv/<証券会社>.yaml` の見出し候補に実際の CSV の見出しを追加すれば、コード変更なしで対応できます。見出しが合わない CSV は取り込まずにエラーにします。

## オートメーション

- ACA ジョブが 15 分ごとに起動し、期限が来たものだけを実行します（1 回最大 20 分）。時刻は日本時間で指定します。
- 1 回の実行で Copilot のリクエストを 1 回以上使います。画面の推定回数と月間上限（既定 300 回、`LH_AUTOMATION_MONTHLY_RUN_LIMIT`）で管理します。
- 既定は読み取り専用です。メモリ・ノート・保有銘柄を書き換えるものだけ「書き換えを許可」をオンにします。
- 使うコネクタは、オートメーションごとに選んだものだけが有効になります。
- GitHub Issue での通知は「GitHub で通知する」をオンにしたものだけです。トークンが無効になったときの再ログイン通知だけは、1 日 1 回自動で送ります。

例: 楽天トラベルの空室チェック

| 項目 | 設定 |
|---|---|
| 指示 | 12/30〜12/31、大人 2 名で施設番号 ○○ の空室を search_rakuten_vacancy で確認して |
| 周期 | 毎日 9:00 |
| コネクタ | 楽天トラベル空室検索 |
| 通知 | GitHub で通知する / ツールの結果の値で判定 `vacancy_count > 0` / 前回は満たさず今回満たしたときだけ |

空室は確認時点のもので、予約は自分で行います。

## コネクタ（外部 API）の追加

1. `app/backend/src/life_helper/connectors/` に `Connector` を継承したクラスを作る（接続先ホスト・必要なシークレット名・呼び出し間隔）。
2. `connectors/registry.py` でツールとして登録する（`ToolSpec(..., connector="名前")`）。
3. キーを URL に含める API は、`copilot_integration/policy.py` の `CONNECTOR_HOSTS` に追加して `web_fetch` から直接呼べないようにする。
4. `config.py` の `Settings`、`infra/*.bicep` のシークレット、`infra/main.parameters.json`、`scripts/setup-cicd.ps1` のシークレット一覧に追加する。
5. 有料のサービスは、使う前に費用を判断する。

## 障害対応

| 場所 | 見るもの |
|---|---|
| 画面「オートメーション」 | 実行履歴（状態・エラー・使ったツール） |
| Azure Portal → ジョブ `caj-lifehelper-…` | 実行履歴 |
| Azure Portal → Log Analytics（`log-…`） | `ContainerAppConsoleLogs_CL`（アプリ・ジョブのログ。秘密情報はマスク済み） |
| GitHub → Actions | CI / Deploy の結果 |
