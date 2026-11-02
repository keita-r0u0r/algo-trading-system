"""Cloud Storage のファイルを、手元のファイルと同期する（見張り役の仮想マシン用。Cloud Run のようなフォルダのつなぎ込みが無いため）。

  * ``pull``: 最新を手元に落とす（世代番号と、落とした時点の長さを覚える）
  * ``push``: 手元で **追記した分だけ** を、最新の内容の後ろに足して上げる。
    上げるときは世代番号を条件にし（ifGenerationMatch）、その間に他が書いていたら、取り直して足し直す。
    他の書き込みを上書きしない
  * 「在る / 無い」だけが意味のファイル（キルスイッチなど）は ``pull_flag`` / ``push_flag``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


@dataclass
class SyncedFile:
    session: object  # google.auth.transport.requests.AuthorizedSession
    bucket: str
    name: str
    local: Path
    _generation: str = "0"
    _pulled_size: int = 0

    def _url(self) -> str:
        return f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(self.name, safe='')}"

    def _download(self) -> tuple[bytes, str]:
        r = self.session.get(self._url(), params={"alt": "media"}, timeout=30)
        if r.status_code == 404:
            return b"", "0"
        r.raise_for_status()
        return r.content, r.headers.get("x-goog-generation", "0")

    def pull(self) -> None:
        content, generation = self._download()
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_bytes(content)
        self._generation, self._pulled_size = generation, len(content)

    def push(self, tries: int = 5) -> bool:
        """手元で足した分を上げる。足した分が無ければ何もしない。"""
        if not self.local.exists():
            return True
        added = self.local.read_bytes()[self._pulled_size:]
        if not added:
            return True
        for _ in range(tries):
            latest, generation = self._download()
            body = latest + added
            r = self.session.post(
                f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
                params={"uploadType": "media", "name": self.name, "ifGenerationMatch": generation},
                data=body, headers={"Content-Type": "application/octet-stream"}, timeout=30)
            if r.status_code == 412:  # その間に他が書いた。取り直して足し直す
                continue
            r.raise_for_status()
            self.local.write_bytes(body)
            self._generation, self._pulled_size = r.json().get("generation", "0"), len(body)
            return True
        return False


@dataclass
class SyncedFlag:
    """在る / 無い だけのファイル（キルスイッチ・ペーパーの停止など）。"""

    session: object
    bucket: str
    name: str
    local: Path
    _remote: bool = field(default=False)

    def _url(self) -> str:
        return f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(self.name, safe='')}"

    def pull(self) -> None:
        r = self.session.get(self._url(), params={"alt": "media"}, timeout=30)
        self._remote = r.status_code == 200
        if self._remote:
            self.local.parent.mkdir(parents=True, exist_ok=True)
            self.local.write_bytes(r.content)
        else:
            self.local.unlink(missing_ok=True)

    def push(self) -> None:
        """手元で新しく立てた印だけを上げる（消すのは画面から。ここでは消さない）。"""
        if self.local.exists() and not self._remote:
            self.session.post(f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
                              params={"uploadType": "media", "name": self.name},
                              data=self.local.read_bytes(), timeout=30)
            self._remote = True
