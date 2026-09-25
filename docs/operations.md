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

## 投資信託の基準価額（運用会社の追加）

基準価額は株価（Yahoo Finance）とは別に、運用会社が公式に公開している API・CSV から取得します（`POST /api/portfolio/refresh-prices` が両方を別々に実行）。
対応済みの運用会社とファンドコード（Phase 1 の公式 API と Phase 2 の公式 CSV の両方に対応済み）:

| 運用会社 | 段階 | 取得元 | ファンドコード | どこで分かるか |
|---|---|---|---|---|
| 三菱UFJアセットマネジメント | Phase 1 | [投信情報 API](https://www.am.mufg.jp/tool/webapi/) | ISIN（12 桁）・投資信託協会コード（8 桁）・ファンドコード（6 桁） | 「取得元を設定」でファンド名から候補を検索できる |
| 楽天投信投資顧問 | Phase 2 | [基準価額の CSV](https://www.rakuten-toushin.co.jp/fund/nav/) | チャート CSV の 6 桁番号 | ファンドページの基準価額 CSV のリンク（`chart_109001.csv` なら `109001`） |
| 大和アセットマネジメント | Phase 2 | [基準価額の CSV](https://www.daiwa-am.co.jp/funds/) | 4 桁のファンドコード | ファンドページの URL（`/funds/detail/3242/detail_top.html` なら `3242`） |

楽天投信投資顧問・大和アセットマネジメントは機械可読なファンド一覧を公開していないため、ファンド名からの候補検索はできません。
「取得元を設定」で運用会社とファンドコードを指定すると、取得できた公式名称を確認したうえで紐付けます。未対応の運用会社を追加する手順:

1. `connectors/fund_nav.py` に `FundNavConnector`（API）または `FundCsvConnector`（公式 CSV）を継承したクラスを作り、
   `provider`・`manager`・`price_unit`・接続先ホストを定義する。
   `fund_nav()` は `fund_code` / `name` / `nav` / `price_unit` / `date` / `source` / `source_url` を返し、`search_funds()` は候補を返す。
   `FundCsvConnector` は `code_shape`（ファンドコードの形式）と `csv_url()` を定義すれば、基準日・基準価額の列を名前で探して最新行を使う。
2. 取得先は公式に案内されている URL 形式だけにする（画面内部の非公開エンドポイントや利用者が入力した URL は取得しない）。
   受け取った基準価額・基準日・ファンドコードは `nav_amount()` / `nav_date()` などで必ず検証する。
3. `market/portfolio.py` の `FundProvider` と `PriceSource` に提供元名を追加し、`connectors/registry.py` に登録する。
4. `app/frontend/src/pages/Portfolio.tsx` の `SOURCE_LABELS` に表示名を追加する。
5. ポートフォリオ画面の「取得元を設定」で、公式名称とファンドコードを確認してから保有銘柄に紐付ける（名前が似ているだけでは紐付けない）。

自動取得に対応していないファンドは「手入力」を選び、公式サイトの基準価額を入力します。取得に失敗したファンドは直前の基準価額を残し、ほかのファンドの更新は続行します。

## 障害対応

| 場所 | 見るもの |
|---|---|
| 画面「オートメーション」 | 実行履歴（状態・エラー・使ったツール） |
| Azure Portal → ジョブ `caj-lifehelper-…` | 実行履歴 |
| Azure Portal → Log Analytics（`log-…`） | `ContainerAppConsoleLogs_CL`（アプリ・ジョブのログ。秘密情報はマスク済み） |
| GitHub → Actions | CI / Deploy の結果 |
