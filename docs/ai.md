# AI の拡張パーツ

Claude や Gemini を使う拡張パーツです。本体とは別に付け外しできます（[extensions.md](extensions.md)）。
呼び出し先は **Vertex AI（既定）** と **Anthropic 直接** から選べます。

> AI の出力は検証の補助であり、**投資助言ではありません。** AI に発注・設定変更の権限はありません。

## できること

| | 役割 | 動き |
|---|---|---|
| A | 分析の相棒 | 検証の記録と運用の状況（集計した数字だけ）をもとに、質問に答える |
| B | 運用の見張り役 | 運用の状況を要約し、異常（想定より大きいずれ、急な変化、条件の後退など）を指摘する |
| C | 売買ルールの案 | これまでの検証を踏まえて、まだ試していない案を決まった形で出す。**実行はしない** |
| D | 売買の判断 | **ペーパー運用でのみ**、翌営業日の売買の候補を1つ出す。既存のリスク制限を必ず通る |

AI を呼ぶのは、画面でボタンを押したときだけです。画面を開いただけでは呼びません。

## 気をつけること

- **C の案は「試行」ではありません。** 実装して検証にかけたときに初めて試行回数 N に数えます。案を大量に出させて片端から試すと、偶然の当たりを拾います
- **D は、モデルが学習した期間の過去データでは先読みになりえます。** モデルはその頃の値動きを知っているためです。評価に使えるのは、モデルの知識の期限より後の、新しいデータで前向きに記録した結果だけです
- AI に渡すのは集計した数字と保有だけです。APIキーなどの秘密は渡しません

## モデルと呼び出し先

モデルは「**呼び出し先:モデルID**」の組（キー）で選びます。同じ Claude でも、呼び出し先で認証・請求・機能が違うためです。

| 呼び出し先 | 中身 | 認証 | 請求 | 断られたときの切り替え |
|---|---|---|---|---|
| `vertex`（既定） | Vertex AI の Claude / Gemini | gcloud のログイン（`GCP_PROJECT`） | Google Cloud | 自前の順番（下記） |
| `anthropic` | Anthropic の API を直接 | Anthropic の APIキー | Anthropic | サーバー側（対応モデル）＋自前の順番 |

既定の一覧（料金は 100万トークンあたりの米ドル、2026年10月時点の公表値。Vertex AI は global エンドポイント）:

| キー | 入力 | 出力 | メモ |
|---|---|---|---|
| `vertex:claude-fable-5-1` | 10.00 | 50.00 | 最も高性能。最も高い |
| `vertex:claude-opus-5-5` | 4.00 | 20.00 | **既定** |
| `vertex:claude-sonnet-5-5` | 2.00 | 10.00 | 速く、安い |
| `vertex:claude-haiku-5-5` | 0.10 | 0.50 | 最も安い |
| `vertex:gemini-4-argon` | 4.00 | 20.00 | 12/1以降の料金で見積もり（それまでは半額） |
| `vertex:gemini-3.8-flash` | 1.50 | 7.50 | 2027年以降の料金で見積もり（それまでは半額） |
| `vertex:gemini-3.1-pro-preview` | 2.00 | 12.00 | プレビュー |
| `anthropic:claude-*` | 同上 | 同上 | Anthropic 直接（Haiku 以外はサーバー側の切り替えあり） |

- **Gemini のモデルID は、料金表の表示名からの推測です。** Model Garden で正しい ID を確かめ、違っていれば画面の「モデルを一覧に足す」で直してください
- Vertex AI でリージョンを指定すると（`AI_VERTEX_REGION`）、Gemini は約1割高くなります。既定は `global`
- 一覧に無いモデルも使えます。料金が分からないので **一覧で最も高い料金で見積もり** ます

画面（AI の画面の「モデルの設定」）でできること:

- **既定のモデル** と、**役割ごとの上書き**（例: B の要約は Haiku、D の判断は Fable）
- **断られたときに次に試すモデル**（上から順）。Vertex AI にはサーバー側の切り替えが無いので、ここで決めます。呼び出し先をまたいでも使えます
- **モデルを一覧に足す**（呼び出し先・種類・モデルID・料金）。`state/ai_models.json` に保存されます

設定は `state/ai_settings.json` に保存されます。環境変数 `AI_MODEL`（例: `vertex:claude-sonnet-5-5`）があれば、保存した既定より優先します。

## モデルの比較

AI の画面の「モデルの比較」で、モデルごとに次を集計します（AI は呼びません）。

| 特徴 | 呼び出し回数、成功率、断られた回数、平均の待ち時間、費用（合計・1回あたり）、評価（1〜5） |
|---|---|
| **D の戦績**（ペーパー） | 判断の内訳（買い・売り・見送り）、確信度、閉じた取引の損益・勝率・t 値、保有中の数量 |

- **評価**: A〜D の答えの下に、星（1〜5）で評価を付けられます
- **損益の結び付け**: D の候補には、どのモデルの判断かが記録されます（理由の先頭の `[AI:<キー>:<ID>]`）。
  銘柄ごとに、先に買ったものから売りに充て（先入れ先出し）、そのモデルの判断で買った株の損益として数えます
- 取引が30件に満たないうちは運の範囲。モデルを比べること自体が多重比較になる。学習期間の違いで先読みの大きさも違う

## 費用

- 1回ごとの費用は、画面と `state/ai_usage.jsonl` に残ります。サーバー側の切り替えが起きたときは、実際に答えたモデルの料金で数えます
- **月の上限**（既定 $5、`AI_MONTHLY_BUDGET_USD`）に達したら AI を呼びません
- 考える深さは役割ごとに決めています（A・B は medium、C・D は high）。Gemini では thinking_level に対応させます
- 思考（thinking）のトークンも出力として課金されます

## 設定（Vertex AI、既定）

```bash
pip install -e ".[ai]"

# Vertex AI の API を有効にし、使うモデルを Model Garden で有効にする（Claude はモデルごとに有効化が要る）
gcloud services enable aiplatform.googleapis.com
gcloud auth application-default login
```

`.env` に次を書きます。

```bash
JQBT_EXTENSIONS=ai
GCP_PROJECT=<プロジェクトID>
AI_VERTEX_REGION=global
AI_MONTHLY_BUDGET_USD=5
```

GUI（`make gui`）のサイドバーに「AI」が出て、AI の画面に準備ができていない旨の表示が出なければ完了です。

Anthropic を直接使うときだけ、APIキーを GCP Secret Manager に置きます（[secrets.md](secrets.md)）。

```bash
gcloud secrets create anthropic-api-key --replication-policy=automatic
read -rs "V?Anthropic の APIキー: "; echo; printf %s "$V" | gcloud secrets versions add anthropic-api-key --data-file=-; unset V
```

**Docker で Vertex AI を使う場合**: コンテナの中には gcloud のログイン情報がありません。
今は未対応です（手元の `make gui` で使ってください）。

## 構成

```
src/jqbt/extensions/ai/
├─ __init__.py     入口。秘密情報（anthropic-api-key）の登録、画面の登録、準備の確認
├─ assistant.py    4つの役割、AI に渡す情報、答えの形（JSON Schema）、費用の上限、評価、断られたときの順番
├─ models.py       モデルの一覧（呼び出し先:モデルID）と料金、利用者が足したモデル、選択の保存
├─ providers.py    呼び出し先への振り分け（Vertex AI の Claude / Gemini、Anthropic 直接）と準備の確認
├─ claude.py       Claude の呼び出し（Anthropic 直接 / Vertex AI）
├─ gemini.py       Gemini の呼び出し（Vertex AI、google-genai）
├─ analytics.py    モデルごとの特徴と D の戦績の集計
├─ usage.py        利用記録（追記のみ）
├─ ports.py        パーツの中だけで使う口（テストでは偽物に差し替える）
└─ gui.py          画面
```

新しい呼び出し先（例: ローカルの Ollama）を足すときは、`models.PROVIDERS` と `providers.py` の `_build` に1つずつ足します。
