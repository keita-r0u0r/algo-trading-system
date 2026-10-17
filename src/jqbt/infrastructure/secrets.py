"""秘密情報（APIキーなど）の取り出し。GCP Secret Manager を主に使い、手元の開発では .env も使える。

## どこから読むか

  * ``GCP_PROJECT`` が設定されていれば GCP Secret Manager（``SECRETS_BACKEND=gcp`` と同じ）
  * そうでなければ ``.env``（``SECRETS_BACKEND=env`` と同じ）
  * ``SECRETS_BACKEND`` を明示すれば、それに従う

**GCP を使う設定で読めないときは、.env に戻らずに止める。** 気づかないうちに
手元の .env のキーで動いていた、という事態を防ぐため。

## 名前

コードの中の名前（``jquants_api_key``）と、.env の変数名、GCP のシークレット名を ``SECRETS`` で対応させる。
GCP 側は ``projects/<project>/secrets/<id>/versions/latest`` を読む。

## 守っていること

  * 値はログ・例外・``repr`` に出さない
  * 読んだ値はプロセスの中でだけ覚える（ファイルに書かない）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


class SecretError(RuntimeError):
    """秘密情報を読めない。メッセージに値は含めない。"""


@dataclass(frozen=True)
class SecretName:
    key: str
    env: str
    gcp_id: str
    description: str


SECRETS: dict[str, SecretName] = {
    s.key: s
    for s in (
        SecretName("jquants_api_key", "JQUANTS_API_KEY", "jquants-api-key", "J-Quants の APIキー"),
        SecretName("gmo_fx_api_key", "GMO_FX_API_KEY", "gmo-fx-api-key", "GMOコイン 外国為替FX の APIキー"),
        SecretName("gmo_fx_api_secret", "GMO_FX_API_SECRET", "gmo-fx-api-secret", "GMOコイン 外国為替FX の APIシークレット"),
    )
}


def _spec(name: str) -> SecretName:
    if name not in SECRETS:
        raise SecretError(f"未知の秘密情報: {name}（{', '.join(SECRETS)}）")
    return SECRETS[name]


class EnvSecrets:
    """.env（環境変数）から読む。手元の開発用。"""

    backend = "env"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def get(self, name: str) -> str | None:
        value = self._environ.get(_spec(name).env, "").strip()
        return value or None

    def describe(self) -> str:
        return ".env（手元の開発用）"

    def __repr__(self) -> str:
        return "EnvSecrets()"


class GcpSecrets:
    """GCP Secret Manager から読む。

    認証は Application Default Credentials。手元では ``gcloud auth application-default login``、
    GCP の仮想マシンでは、そのマシンのサービスアカウントの権限で読む。コードは同じ。
    """

    backend = "gcp"

    def __init__(self, project: str | None, client: Any = None) -> None:
        if not project:
            raise SecretError("GCP_PROJECT が設定されていません（GCP Secret Manager を使う場合は必須）")
        self.project = project
        self._client = client
        self._cache: dict[str, str | None] = {}

    def _connect(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise SecretError(
                    'GCP Secret Manager のライブラリがありません。pip install -e ".[gcp]" を実行してください'
                ) from exc
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def get(self, name: str) -> str | None:
        if name in self._cache:
            return self._cache[name]
        secret_id = _spec(name).gcp_id
        path = f"projects/{self.project}/secrets/{secret_id}/versions/latest"
        try:
            response = self._connect().access_secret_version(request={"name": path})
        except SecretError:
            raise
        except Exception as exc:  # NotFound / PermissionDenied / 認証切れ など
            kind = type(exc).__name__
            if kind == "NotFound":
                self._cache[name] = None
                return None
            raise SecretError(
                f"GCP Secret Manager から {secret_id} を読めません（{kind}）。"
                "gcloud のログイン（application-default）と、シークレットへのアクセス権を確認してください"
            ) from None
        # gcloud に echo で入れると末尾に改行が付くことがあるので落とす
        value = response.payload.data.decode("utf-8").strip() or None
        self._cache[name] = value
        return value

    def describe(self) -> str:
        return f"GCP Secret Manager（{self.project}）"

    def __repr__(self) -> str:
        return f"GcpSecrets(project={self.project!r})"


def build_secrets(environ: Mapping[str, str] | None = None, client: Any = None) -> EnvSecrets | GcpSecrets:
    """設定に従って、取り出し口を作る。"""
    environ = os.environ if environ is None else environ
    backend = (environ.get("SECRETS_BACKEND") or ("gcp" if environ.get("GCP_PROJECT") else "env")).lower()
    if backend == "gcp":
        return GcpSecrets(environ.get("GCP_PROJECT"), client=client)
    if backend == "env":
        return EnvSecrets(environ)
    raise SecretError(f"SECRETS_BACKEND は gcp か env です（指定: {backend}）")


def require(source: Any, name: str) -> str:
    """必ず要る秘密情報。無ければ、どこに置けばよいかを添えて止める。"""
    value = source.get(name)
    if value:
        return value
    spec = _spec(name)
    where = (
        f"GCP Secret Manager のシークレット {spec.gcp_id}（プロジェクト {getattr(source, 'project', '?')}）"
        if source.backend == "gcp"
        else f".env の {spec.env}"
    )
    raise SecretError(f"{spec.description} がありません。{where} に設定してください")
