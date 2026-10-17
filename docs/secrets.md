# 秘密情報（APIキーなど）の管理

APIキーなどの秘密情報は、**GCP Secret Manager** に置くことを前提にしています。
手元の開発では `.env` も使えますが、実運用（GMOコインの発注など）では Secret Manager を使ってください。

## どこから読むか

| 設定 | 読む場所 |
|---|---|
| `GCP_PROJECT` を設定（推奨） | GCP Secret Manager |
| 何も設定しない | `.env`（手元の開発用） |
| `SECRETS_BACKEND=gcp` / `env` | 明示した方 |

**GCP を使う設定で読めないときは、`.env` に戻らずに止まります。** 気づかないうちに手元の `.env` のキーで動いていた、という事態を防ぐためです。

値はログ・エラー・画面に出しません。画面（GUI）のサイドバーには、置き場所と「設定済み / 未設定」だけが出ます。

## 対象

| コードの中の名前 | GCP のシークレット名 | `.env` の変数名 |
|---|---|---|
| `jquants_api_key` | `jquants-api-key` | `JQUANTS_API_KEY` |
| `gmo_fx_api_key` | `gmo-fx-api-key` | `GMO_FX_API_KEY` |
| `gmo_fx_api_secret` | `gmo-fx-api-secret` | `GMO_FX_API_SECRET` |

## GCP 側の準備（初回だけ）

前提: GCP のプロジェクトがあり、課金が有効になっていること（Secret Manager は少量なら無料枠に収まることが多いですが、料金は公式で確認してください）。

```bash
# 1. gcloud を入れてログインし、プロジェクトを選ぶ
brew install --cask google-cloud-sdk
gcloud auth login
gcloud config set project <プロジェクトID>

# 2. Secret Manager を有効にする
gcloud services enable secretmanager.googleapis.com

# 3. シークレットを作る（値はまだ入れない）
gcloud secrets create jquants-api-key --replication-policy=automatic
gcloud secrets create gmo-fx-api-key --replication-policy=automatic
gcloud secrets create gmo-fx-api-secret --replication-policy=automatic
```

値は、**画面に表示せず、シェルの履歴にも残らない**入れ方をします（zsh）。

```bash
read -rs "V?J-Quants の APIキー: "; echo; printf %s "$V" | gcloud secrets versions add jquants-api-key --data-file=-; unset V
```

GMOコインのキーとシークレットも、シークレット名を替えて同じように入れます。

```bash
# 4. このマシンのプログラムが、ログインしたアカウントの権限で読めるようにする
gcloud auth application-default login

# 5. このリポジトリで GCP を使う設定にする
pip install -e ".[gcp]"
echo "GCP_PROJECT=<プロジェクトID>" >> .env
```

`make fetch` で `APIキーの読み込み元: GCP Secret Manager（<プロジェクトID>）` と出れば完了です。
そのあと、`.env` に残っている `JQUANTS_API_KEY` の値は消してください。

## キーを差し替えるとき

新しい値を `versions add` で足すと、それが最新として読まれます。古い版は残るので、問題があれば戻せます。
差し替えが済んだら、古い版を無効にします。

```bash
gcloud secrets versions list jquants-api-key
gcloud secrets versions disable <古い版の番号> --secret=jquants-api-key
```

## 将来、GCP の仮想マシンに移すとき

コードは変えずに動きます。仮想マシンには専用のサービスアカウントを付け、**必要なシークレットだけ**に
「Secret Manager のシークレット アクセサー」（`roles/secretmanager.secretAccessor`）を与えます。
プロジェクト全体の権限は与えません。
