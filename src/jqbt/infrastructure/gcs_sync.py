"""Cloud Storage のファイルを、手元のファイルと同期する（見張り役の仮想マシン用。Cloud Run のようなフォルダのつなぎ込みが無いため）。

  * ``pull``: 最新を手元に落とす（世代番号と、落とした時点の長さを覚える）。追記だけのファイル（``append_only``）は、
    変わっていなければ何も運ばず、増えていれば増えた分だけを取る
  * ``push``: 手元で **追記した分だけ** を、最新の内容の後ろに足して上げる。
    上げるときは世代番号を条件にし（ifGenerationMatch）、その間に他が書いていたら、揃え直して足し直す。
    他の書き込みを上書きしない。追記だけのファイルは、足した分だけを上げてクラウドの中でつなぐ（compose）

見張り役は米国、バケットは東京にある。大陸をまたぐ通信は量に応じて課金されるので、大きくなる記録を毎回丸ごと運ばない。
  * 「在る / 無い」だけが意味のファイル（キルスイッチなど）は ``pull_flag`` / ``push_flag``
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
UPLOAD_OBJECT_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}"
OVERLAP = 256  # 増えた分を取るときに、書き換えられていないことを確かめるため一緒に取る末尾の長さ


@dataclass
class SyncedFile:
    session: object  # google.auth.transport.requests.AuthorizedSession
    bucket: str
    name: str
    local: Path
    #: 追記しかされないファイル（*.jsonl）。増えた分だけをやり取りする。JSON のように中身が入れ替わるものは False のまま
    append_only: bool = False
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

    def _metadata(self) -> dict | None:
        """長さと世代番号だけを見る（中身は運ばない）。無ければ None。"""
        r = self.session.get(self._url(), timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def _local_base(self) -> bytes | None:
        """前回クラウドと揃えた分（まだ上げていない追記を除く）。手元が欠けていれば None。"""
        if not self.local.exists():
            return None
        data = self.local.read_bytes()
        return data[:self._pulled_size] if len(data) >= self._pulled_size else None

    def _grown(self, meta: dict) -> bytes | None:
        """クラウドの今の中身を、手元に足りない分だけ取って組み立てる。追記とみなせないときは None（丸ごと取り直す）。

        手元の末尾の少しも一緒に取って比べ、書き換えられていないことを確かめる。世代番号を指定して、取る間の書き込みに影響されない。
        """
        size = int(meta["size"])
        base = self._local_base()
        if not self.append_only or base is None or not base or size < len(base):
            return None
        start = max(0, len(base) - OVERLAP)
        r = self.session.get(self._url(), params={"alt": "media", "generation": meta["generation"]},
                             headers={"Range": f"bytes={start}-"}, timeout=30)
        if r.status_code != 206 or r.content[:len(base) - start] != base[start:]:
            return None
        body = base + r.content[len(base) - start:]
        return body if len(body) == size else None

    def pull(self) -> None:
        if not self.append_only:  # 小さい設定のファイルは、これまでどおり毎回クラウドの中身に揃える
            self._store(*self._download())
            return
        meta = self._metadata()
        if meta is None:
            self._store(b"", "0")
            return
        base = self._local_base()
        if meta["generation"] == self._generation and base is not None and self._pulled_size:
            if self.local.stat().st_size != len(base):  # 上げていない分は持ち越さない（クラウドに揃える）
                self.local.write_bytes(base)
            return  # 中身は同じ。運ばない
        body = self._grown(meta)
        self._store(*(self._download() if body is None else (body, meta["generation"])))

    def _store(self, content: bytes, generation: str) -> None:
        self._generation, self._pulled_size = generation, len(content)
        if generation == "0":
            # クラウドに無い。空のファイルを作ると、読む側が壊れた JSON として失敗するので、手元からも消す
            self.local.unlink(missing_ok=True)
            return
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_bytes(content)

    def push(self, tries: int = 5) -> bool:
        """手元で足した分を上げる。足した分が無ければ何もしない。"""
        if not self.local.exists():
            return True
        added = self.local.read_bytes()[self._pulled_size:]
        if not added:
            return True
        return self._push_appended(added, tries) if self.append_only else self._push_whole(added, tries)

    def _push_whole(self, added: bytes, tries: int) -> bool:
        for _ in range(tries):
            latest, generation = self._download()
            if self._upload_whole(latest + added, generation):
                return True
        return False

    def _upload_whole(self, body: bytes, generation: str) -> bool:
        r = self.session.post(
            UPLOAD_URL.format(bucket=self.bucket),
            params={"uploadType": "media", "name": self.name, "ifGenerationMatch": generation},
            data=body, headers={"Content-Type": "application/octet-stream"}, timeout=30)
        if r.status_code == 412:  # その間に他が書いた。取り直して足し直す
            return False
        r.raise_for_status()
        self.local.write_bytes(body)
        self._generation, self._pulled_size = r.json().get("generation", "0"), len(body)
        return True

    def _push_appended(self, added: bytes, tries: int) -> bool:
        """足した分だけを一時的なオブジェクトとして上げ、クラウドの中で本体の後ろにつなぐ（compose）。本体は運ばない。"""
        part = f"tmp/appends/{self.name.rsplit('/', 1)[-1]}.{uuid.uuid4().hex}"
        uploaded = False
        try:
            for _ in range(tries):
                meta = self._metadata()
                if meta is None:  # まだ無いファイルは、そのまま作る
                    if self._upload_whole(added, "0"):
                        return True
                    continue
                # 他が書いた分も手元に揃える。手元と順番が同じになるように、先に揃えてから足す
                base = self._local_base() if meta["generation"] == self._generation else self._grown(meta)
                if base is None:
                    base = self._download_generation(meta["generation"])
                    if base is None:  # 取る間に次の世代になった
                        continue
                if not uploaded:
                    r = self.session.post(UPLOAD_URL.format(bucket=self.bucket),
                                          params={"uploadType": "media", "name": part}, data=added,
                                          headers={"Content-Type": "application/octet-stream"}, timeout=30)
                    r.raise_for_status()
                    uploaded = True
                r = self.session.post(
                    f"{self._url()}/compose", params={"ifGenerationMatch": meta["generation"]},
                    json={"sourceObjects": [{"name": self.name}, {"name": part}],
                          "destination": {"contentType": "application/octet-stream"}}, timeout=30)
                if r.status_code == 412:  # その間に他が書いた。揃え直して足し直す
                    continue
                r.raise_for_status()
                body = base + added
                result = r.json()
                self.local.write_bytes(body)
                self._generation, self._pulled_size = result.get("generation", "0"), len(body)
                if int(result.get("size", len(body))) != len(body):  # 食い違ったら、クラウドの中身を正とする
                    self._generation = "0"
                    self.pull()
                return True
            return False
        finally:
            if uploaded:
                try:
                    self.session.delete(UPLOAD_OBJECT_URL.format(bucket=self.bucket, name=quote(part, safe="")), timeout=30)
                except Exception:  # noqa: BLE001  消せなくても記録は上がっている。小さな一時ファイルが残るだけ
                    pass

    def _download_generation(self, generation: str) -> bytes | None:
        r = self.session.get(self._url(), params={"alt": "media", "generation": generation}, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content


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
