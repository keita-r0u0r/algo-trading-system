"""Cloud Storage との同期: 他の書き込みを上書きしない。通信は偽物。"""

from __future__ import annotations

from jqbt.infrastructure.gcs_sync import SyncedFile


class FakeResponse:
    def __init__(self, status, content=b"", headers=None, data=None):
        self.status_code, self.content, self.headers, self._data = status, content, headers or {}, data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._data


class FakeGcs:
    """1つのオブジェクトだけを持つ偽物。世代番号が合わない上書きは 412。"""

    def __init__(self, content: bytes):
        self.content, self.generation = content, 1
        self.race = None  # 次の書き込みの直前に、他が書き込む内容

    def get(self, url, params=None, timeout=None):
        return FakeResponse(200, self.content, {"x-goog-generation": str(self.generation)})

    def post(self, url, params=None, data=None, headers=None, timeout=None):
        if self.race is not None:
            self.content, self.generation, self.race = self.content + self.race, self.generation + 1, None
        if params.get("ifGenerationMatch") != str(self.generation):
            return FakeResponse(412)
        self.content, self.generation = data, self.generation + 1
        return FakeResponse(200, data={"generation": str(self.generation)})


def test_push_appends_only_new_lines_and_keeps_others_writes(tmp_path):
    gcs = FakeGcs(b"a\n")
    f = SyncedFile(gcs, "b", "state/x.jsonl", tmp_path / "x.jsonl")
    f.pull()
    with f.local.open("a") as handle:
        handle.write("mine\n")
    gcs.race = b"theirs\n"  # 上げる前に、他（ジョブ）が書いた
    assert f.push()
    assert gcs.content == b"a\ntheirs\nmine\n"  # どちらも残る
    assert f.push()  # 足した分が無ければ何もしない
    assert gcs.content == b"a\ntheirs\nmine\n"
