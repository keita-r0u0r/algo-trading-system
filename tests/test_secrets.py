"""秘密情報の取り出し。GCP には繋がず、偽のクライアントで約束を固定する。

  * GCP を使う設定なら GCP から読み、.env には戻らない
  * 値はエラーにも repr にも出ない
  * 末尾の改行を落とす、無いシークレットは None
"""

from __future__ import annotations

import pytest

from jqbt.infrastructure.secrets import (
    EnvSecrets,
    GcpSecrets,
    SecretError,
    build_secrets,
    require,
)

SECRET_VALUE = "sk-live-very-secret-value"


class NotFound(Exception):
    pass


class PermissionDenied(Exception):
    pass


class FakeClient:
    def __init__(self, store: dict[str, str] | None = None, error: Exception | None = None):
        self.store = store or {}
        self.error = error
        self.calls: list[str] = []

    def access_secret_version(self, request):
        name = request["name"]
        self.calls.append(name)
        if self.error:
            raise self.error
        secret_id = name.split("/secrets/")[1].split("/")[0]
        if secret_id not in self.store:
            raise NotFound(name)

        class Payload:
            data = self.store[secret_id].encode("utf-8")

        class Response:
            payload = Payload()

        return Response()


def test_gcp_project_selects_secret_manager_and_reads_latest_version():
    client = FakeClient({"jquants-api-key": SECRET_VALUE + "\n"})
    source = build_secrets({"GCP_PROJECT": "my-proj"}, client=client)

    assert source.backend == "gcp"
    assert source.get("jquants_api_key") == SECRET_VALUE  # 末尾の改行は落とす
    assert client.calls == ["projects/my-proj/secrets/jquants-api-key/versions/latest"]


def test_values_are_cached_within_the_process():
    client = FakeClient({"jquants-api-key": SECRET_VALUE})
    source = GcpSecrets("p", client=client)
    source.get("jquants_api_key")
    source.get("jquants_api_key")
    assert len(client.calls) == 1


def test_gcp_never_falls_back_to_the_env_file():
    """GCP を使う設定なら、.env にキーがあっても使わない。"""
    environ = {"GCP_PROJECT": "p", "JQUANTS_API_KEY": "from-dotenv"}
    source = build_secrets(environ, client=FakeClient({}))
    assert source.get("jquants_api_key") is None
    with pytest.raises(SecretError, match="jquants-api-key"):
        require(source, "jquants_api_key")


def test_permission_errors_stop_without_leaking_the_value():
    source = GcpSecrets("p", client=FakeClient(error=PermissionDenied("denied")))
    with pytest.raises(SecretError, match="PermissionDenied") as info:
        source.get("gmo_fx_api_key")
    assert SECRET_VALUE not in str(info.value)


def test_repr_does_not_show_values():
    source = GcpSecrets("p", client=FakeClient({"jquants-api-key": SECRET_VALUE}))
    source.get("jquants_api_key")
    assert SECRET_VALUE not in repr(source)
    assert SECRET_VALUE not in repr(EnvSecrets({"JQUANTS_API_KEY": SECRET_VALUE}))


def test_env_backend_is_used_without_a_gcp_project():
    source = build_secrets({"JQUANTS_API_KEY": SECRET_VALUE})
    assert source.backend == "env"
    assert source.get("jquants_api_key") == SECRET_VALUE
    assert source.get("gmo_fx_api_key") is None


def test_explicit_backend_wins_and_unknown_backend_is_rejected():
    assert build_secrets({"SECRETS_BACKEND": "env", "GCP_PROJECT": "p"}).backend == "env"
    with pytest.raises(SecretError, match="gcp か env"):
        build_secrets({"SECRETS_BACKEND": "vault"})
    with pytest.raises(SecretError, match="GCP_PROJECT"):
        build_secrets({"SECRETS_BACKEND": "gcp"})


def test_unknown_secret_names_are_rejected():
    with pytest.raises(SecretError, match="未知"):
        EnvSecrets({}).get("password")


def test_require_says_where_to_put_the_secret():
    with pytest.raises(SecretError, match=r"\.env の GMO_FX_API_SECRET"):
        require(EnvSecrets({}), "gmo_fx_api_secret")


def test_missing_library_gives_an_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("google.cloud"):
            raise ImportError("no google")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SecretError, match=r"\.\[gcp\]"):
        GcpSecrets("p").get("jquants_api_key")
