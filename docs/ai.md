# AI（Claude）の拡張パーツ

Claude（Anthropic の API）を使う拡張パーツです。本体とは別に付け外しできます（[extensions.md](extensions.md)）。

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

## 費用

- モデルは Claude Opus 5.5（100万トークンあたり 入力 $4 / 出力 $20、2026年10月時点の公表値）
- **月の上限**を決めておき、達したら AI を呼びません（既定 $5。`AI_MONTHLY_BUDGET_USD` で変更）
- 1回ごとの費用は画面に表示され、`state/ai_usage.jsonl` に記録されます
- 考える深さ（effort）は役割ごとに決めています（A・B は medium、C・D は high）
- 共通の前提（system）にキャッシュの印を付けています。条件を満たせば、繰り返しの呼び出しが安くなります
- 安全上の理由で答えが控えられたときは、Anthropic 側で推奨の別モデルに自動で切り替えます（`fallbacks: "default"`）。
  それでも控えられたら、理由を添えて止まります

## 設定

```bash
pip install -e ".[ai]"
```

Anthropic の APIキー（https://console.anthropic.com/ で発行）を、GCP Secret Manager に置きます（[secrets.md](secrets.md)）。

```bash
gcloud secrets create anthropic-api-key --replication-policy=automatic
read -rs "V?Anthropic の APIキー: "; echo; printf %s "$V" | gcloud secrets versions add anthropic-api-key --data-file=-; unset V
```

`.env` に次を書きます。

```bash
JQBT_EXTENSIONS=ai
AI_MONTHLY_BUDGET_USD=5
```

GUI（`make gui`、Docker なら `make docker-gui`）のサイドバーに「AI（Claude）」が出れば完了です。

## 構成

```
src/jqbt/extensions/ai/
├─ __init__.py     入口。秘密情報（anthropic-api-key）の登録、画面の登録
├─ assistant.py    4つの役割、AI に渡す情報の組み立て、答えの形（JSON Schema）、費用の上限
├─ claude.py       Claude の呼び出し（構造化出力・effort・キャッシュ・フォールバック・エラーの扱い）
├─ usage.py        利用記録（追記のみ）
├─ ports.py        パーツの中だけで使う口（テストでは偽物に差し替える）
└─ gui.py          画面
```
