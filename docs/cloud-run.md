# Cloud Run で動かす

運用の操作盤（GUI）を Cloud Run で動かし、スマホからも開けるようにします。
作るものはすべて `infra/`（OpenTofu。Terraform と互換）に書いてあり、手では作りません。

> 注文を出す仕組みはありません。クラウドに置くのは、画面とその記録だけです。

## 全体

```
スマホ / PC ──(Google のログイン)── IAP ── Cloud Run（1台まで）
                                              │  サービス専用のアカウント
                                              ├─ Vertex AI（AI）
                                              ├─ Secret Manager（秘密情報）
                                              └─ Cloud Storage（非公開のバケット）
                                                   ├─ state/      設定・AI の記録・キルスイッチ（直接つなぐ）
                                                   ├─ data/       相場データ（読み取り専用でつなぐ）
                                                   └─ litestream/ 台帳の複製
```

| 何を | どうする | なぜ |
|---|---|---|
| 開けるのは自分だけ | IAP。`allowed_users` のアカウントでログインしたときだけ通す | アドレスを知られても開けない |
| 台帳（SQLite） | コンテナの中に置き、Litestream で数秒おきにバケットへ複製。起動時に戻す | バケットを直接つないだフォルダには、SQLite に要るファイルのロックが無い |
| 動かすのは1台まで | `max_instance_count = 1` | 2台で台帳を書くと複製が食い違う |
| 画面を開いていない間 | 0台に縮む（費用がかからない）。次に開くと起動に十数秒かかる | 費用を抑える |
| 記録の正本 | **クラウド**。手元の `state/` は最初に1回上げたあとは使わない | 両方で書くと食い違う |
| FX のペーパー運用 | Cloud Run のジョブを、Cloud Scheduler が月〜土の毎時1分に起こす（[fx-paper.md](fx-paper.md)） | 仕事中も判断を記録し続ける |

バケットはバージョン管理を有効にしてあり、消したり上書きしたりした記録は30日まで戻せます。
OpenTofu からも消せないようにしてあります（`prevent_destroy`）。

## 費用

画面を開いている間だけ動きます。1日1時間ほどなら Cloud Run の無料の枠に収まります。
イメージ（最近の5つ）とバケット（約150MB）は月に数円です。予算のアラート（月1000円）の対象に入ります。

## 最初の1回

```bash
brew install opentofu
cp infra/terraform.tfvars.example infra/terraform.tfvars   # プロジェクトIDと、開けるアカウントを書く
make infra-init      # 状態（tfstate）の置き場所を作って、つなぐ
make infra-apply     # API・バケット・イメージの置き場所・権限を作る（Cloud Run はまだ作らない）
make cloud-seed      # 手元の記録と相場データをバケットに上げる
make deploy          # イメージを作って、Cloud Run を作る（IAP 付き）
tofu -chdir=infra output url
```

表示されたアドレスを、`allowed_users` のアカウントで開きます。

### 組織に属していないプロジェクトの IAP

個人の Google アカウントで作ったプロジェクト（組織なし）では、Google が用意する OAuth クライアントが使えません。
そのままだと、画面を開いたときに `Empty Google Account OAuth client ID(s)/secret(s).` と出ます。自分で作った OAuth クライアントを IAP に設定します。

1. コンソールの「Google Auth Platform」で、同意画面を作る（ユーザーの種類は「外部」。テストユーザーに自分のアカウントを足す）
2. 「クライアント」で、種類「ウェブ アプリケーション」の OAuth クライアントを作る
3. 作ったクライアントの「承認済みのリダイレクト URI」に、次を足す（`<クライアントID>` は作ったものに置き換える）

   ```
   https://iap.googleapis.com/v1/oauth/clientIds/<クライアントID>:handleRedirect
   ```

4. シークレットを Secret Manager に置く（値は画面に出ない）

   ```bash
   gcloud secrets create iap-oauth-client-secret --replication-policy=automatic
   read -rs "V?OAuth クライアントのシークレット: "; echo; printf %s "$V" | gcloud secrets versions add iap-oauth-client-secret --data-file=-; unset V
   ```

5. `infra/terraform.tfvars` にクライアントID を書き、`make infra-apply` する

   ```hcl
   iap_oauth_client_id = "xxxxxxxx.apps.googleusercontent.com"
   ```

シークレットは OpenTofu の状態（非公開のバケット `<プロジェクト>-tfstate`）にも残ります。

## 更新する

```bash
make deploy          # 今のコードでイメージを作り直し、その版で動かす
make infra-plan      # 権限やバケットの変更だけを確かめる（版は変えない）
make infra-apply     # それを反映する
```

`infra-plan` / `infra-apply` は、前回 `deploy` した版をそのまま使います。

更新の瞬間は、古い台と新しい台が短く重なります。その間に画面で記録を付けると、その記録が複製されないことがあります。
**更新中は操作しないでください。**

## FX のペーパー運用を止める・変える

`infra/terraform.tfvars` に書いて `make infra-apply` します。

```hcl
fx_paper_paused = true      # 1時間ごとの実行を止める
fx_paper_risk   = "small"   # リスクの度合い（small / medium / max）
fx_paper_schedule = "1 9-18 * * 1-5"   # 動かす時刻（cron、日本時間）。例は平日の9〜18時の毎時1分
```

ジョブと画面は、同じバケットの `state/` を読み書きします。AI の利用記録（`ai_usage.jsonl`）は両方が書くので、
同じ瞬間に書くと1行失うことがあります（ファイルのロックが無いため）。費用の上限の数え方が少し甘くなる程度です。

## 手元との関係

- `make gui` は今まで通り、手元の `state/` を使います（クラウドとは別の記録）
- AI の費用の上限（月 $5）は、手元とクラウドで別々に数えます
- 相場データを取り込み直したら、`make cloud-seed` でバケットにも上げます（台帳は上書きしません。`state/seed/` は初回だけ使います）

## 止める・消す

```bash
gcloud run services update algo-trading-gui --region=asia-northeast1 --max-instances=0   # 一時的に止める
```

消すときは `infra/main.tf` の Cloud Run を消してから `make infra-apply` します。記録のバケットは残ります。
