# FX の自動売買（実際のお金）

ペーパー運用の口座の判断を、GMOコインの実際の口座で **小さく** まねます。

**方針**: 止めるのは、致命的な損失（入金額を超える損＝不足金）のおそれがあるときだけ。持ち続ける理由が無くなった建玉は閉じる（ルールの条件が崩れた、AI が閉じると判断した、損切りの値段に届いた）。
**実際にお金が動きます。** 結果は保証されず、損をすることがあります。投資の助言ではありません。

## まねるもの

| 口座 | 実際の注文 |
|---|---|
| AI | ペーパー運用で建てたら、同じ向きに **100通貨まで** 成行で建てる。損切り・利確は同じ幅で GMOコイン側に置く |
| 金利差＋流れ（ドル円） | 同上（100通貨、損切り150 pips） |

- ペーパー運用が判断で閉じたら、実際の建玉も成行で決済します
- ペーパー運用の損切り・利確に掛かったときは、GMOコイン側に置いた注文で決済されます（システムが止まっていても効く）
- 15分ごとのペーパー運用の回に、まとめて行います（`scripts/fx_live_trade.py`）

## 安全装置

| 装置 | 動き |
|---|---|
| 有効にするスイッチ | `infra/terraform.tfvars` の `fx_live_enabled = true` のときだけ注文を出す。既定は false（何をするかを記録するだけ） |
| キルスイッチ | 画面「FX → 実際の約定」のボタン。入れると新しい注文を出さない（決済は出す） |
| 数量の上限 | 1口座100通貨まで。ペーパー運用がそれより大きく建てても、100通貨に切る |
| 建玉は1口座1つ | 持っている間は新しく建てない |
| 致命的な損失を防ぐ | 証拠金維持率が150%を切ったら（評価額が0以下でも）、全部の建玉を成行で決済し、キルスイッチを入れる。急変でロスカット（100%）を飛び越え、入金額を超える損（不足金）が出るのを防ぐ |
| 1日の損失の上限 | **既定では使わない**（利用者の方針: 致命的でない損失では止めない）。`LiveConfig.daily_loss_limit` で入れられる |
| 証拠金の余裕 | 取引余力が、必要な証拠金（新規は20倍）の1.5倍に足りなければ建てない |
| 損切りを置けない | 3回試してもだめなら、その建玉を成行で決済し、キルスイッチを入れる |
| 約定を確かめられない | キルスイッチを入れる（アプリで建玉を確かめる） |
| 二重発注の防止 | 判断ごとに一意な顧客注文ID |
| 発注用のキーを分ける | 照会用とは別のキー（gmo-fx-trade-api-key / secret）。照会用の部品には発注の機能が無い（テストで固定） |
| IP の制限 | クラウドの出口の IP を固定し、発注用のキーをその IP だけに制限する |

## 始め方

1. 出口の IP を固定する（有料。固定 IP と Cloud NAT で月に数百円の見込み）

   ```hcl
   # infra/terraform.tfvars
   fx_live_egress = true
   ```

   `make infra-apply` のあと、`tofu -chdir=infra output egress_ip` で IP を確かめる

2. GMOコインで **発注用の** API キーを作る。権限は照会と注文（入出金は付けない）。**IP の制限に 1. の IP を登録する**
3. キーを Secret Manager に置く（ターミナルで。値は画面に出ない）

   ```bash
   gcloud secrets create gmo-fx-trade-api-key --replication-policy=automatic
   read -rs "V?発注用の APIキー: "; echo; printf %s "$V" | gcloud secrets versions add gmo-fx-trade-api-key --data-file=-; unset V
   gcloud secrets create gmo-fx-trade-api-secret --replication-policy=automatic
   read -rs "V?発注用のシークレット: "; echo; printf %s "$V" | gcloud secrets versions add gmo-fx-trade-api-secret --data-file=-; unset V
   ```

4. 外国為替FX の口座に資金がある（取引余力が見える）ことを、画面「実際の約定」で確かめる
5. `fx_live_enabled = true` にして `make infra-apply`

止めるときは、画面のキルスイッチ（すぐ効く）か、`fx_live_enabled = false` で `make infra-apply`。
