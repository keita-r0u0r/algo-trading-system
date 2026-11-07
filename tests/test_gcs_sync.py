"""Cloud Storage との同期: 他の書き込みを上書きしない。大きい記録を丸ごと運ばない。通信は偽物。"""

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
    """本体と、一時的なオブジェクトを持つ偽物。世代番号が合わない上書きは 412。運んだ量を数える。"""

    def __init__(self, content: bytes | None, name: str = "state/x.jsonl"):
        self.name, self.content, self.generation = name, content, 1
        self.old: dict[str, bytes] = {} if content is None else {"1": content}
        self.parts: dict[str, bytes] = {}
        self.race = None  # 次の書き込みの直前に、他が書き込む内容
        self.down = self.up = 0
        self.deleted: list[str] = []

    def _set(self, content: bytes) -> None:
        self.content, self.generation = content, self.generation + 1
        self.old[str(self.generation)] = content

    def get(self, url, params=None, headers=None, timeout=None):
        if self.content is None:
            return FakeResponse(404)
        params = params or {}
        if params.get("alt") != "media":
            return FakeResponse(200, data={"generation": str(self.generation), "size": str(len(self.content))})
        generation = params.get("generation", str(self.generation))
        if generation not in self.old:
            return FakeResponse(404)
        body = self.old[generation]
        if headers and "Range" in headers:
            start = int(headers["Range"].removeprefix("bytes=").removesuffix("-"))
            if start >= len(body):
                return FakeResponse(416)
            self.down += len(body) - start
            return FakeResponse(206, body[start:], {"x-goog-generation": generation})
        self.down += len(body)
        return FakeResponse(200, body, {"x-goog-generation": generation})

    def post(self, url, params=None, data=None, json=None, headers=None, timeout=None):
        params = params or {}
        if url.endswith("/compose"):
            if self.race is not None:
                self._set(self.content + self.race)
                self.race = None
            if params.get("ifGenerationMatch") != str(self.generation):
                return FakeResponse(412)
            names = [s["name"] for s in json["sourceObjects"]]
            assert names[0] == self.name
            self._set(self.content + b"".join(self.parts[n] for n in names[1:]))
            return FakeResponse(200, data={"generation": str(self.generation), "size": str(len(self.content))})
        self.up += len(data)
        if params["name"] != self.name:
            self.parts[params["name"]] = data
            return FakeResponse(200, data={"generation": "1"})
        if self.race is not None:
            self._set(self.content + self.race)
            self.race = None
        current = "0" if self.content is None else str(self.generation)
        if params.get("ifGenerationMatch") != current:
            return FakeResponse(412)
        self._set(data)
        return FakeResponse(200, data={"generation": str(self.generation)})

    def delete(self, url, timeout=None):
        self.deleted.append(url)
        return FakeResponse(204)


def synced(gcs, tmp_path, append_only=True):
    return SyncedFile(gcs, "b", gcs.name, tmp_path / "x.jsonl", append_only=append_only)


def add(f, text):
    with f.local.open("a") as handle:
        handle.write(text)


def test_push_appends_only_new_lines_and_keeps_others_writes(tmp_path):
    for append_only in (True, False):
        gcs = FakeGcs(b"a\n")
        f = synced(gcs, tmp_path, append_only)
        f.pull()
        add(f, "mine\n")
        gcs.race = b"theirs\n"  # 上げる前に、他（ジョブ）が書いた
        assert f.push()
        assert gcs.content == b"a\ntheirs\nmine\n"  # どちらも残る
        assert f.local.read_bytes() == gcs.content  # 手元も同じ順番
        assert f.push()  # 足した分が無ければ何もしない
        assert gcs.content == b"a\ntheirs\nmine\n"


def test_race_seen_before_compose_keeps_local_in_cloud_order(tmp_path):
    gcs = FakeGcs(b"a\n")
    f = synced(gcs, tmp_path)
    f.pull()
    add(f, "mine\n")
    gcs._set(gcs.content + b"theirs\n")  # 上げ始める前に、他が書いた
    assert f.push()
    assert gcs.content == f.local.read_bytes() == b"a\ntheirs\nmine\n"
    add(f, "next\n")  # 次の追記も正しい位置から
    assert f.push()
    assert gcs.content == b"a\ntheirs\nmine\nnext\n"


def test_large_log_is_not_carried_whole_each_cycle(tmp_path):
    """大きい記録でも、判断の回（取る→足す→上げる）で運ぶのは増えた分だけ。"""
    big = b"x" * 10_000_000 + b"\n"
    gcs = FakeGcs(big)
    f = synced(gcs, tmp_path)
    f.pull()
    gcs.down = gcs.up = 0
    for i in range(10):
        f.pull()
        add(f, f"line {i}\n")
        assert f.push()
    assert gcs.down == 0
    assert gcs.up < 100
    assert gcs.content == f.local.read_bytes()
    assert len(gcs.deleted) == 10  # 一時的なオブジェクトを残さない


def test_pull_takes_only_what_others_added(tmp_path):
    big = b"x" * 1_000_000 + b"\n"
    gcs = FakeGcs(big)
    f = synced(gcs, tmp_path)
    f.pull()
    gcs.down = 0
    gcs._set(gcs.content + b"theirs\n")
    f.pull()
    assert f.local.read_bytes() == gcs.content
    assert gcs.down < 1000


def test_rewritten_or_shrunk_cloud_file_is_fetched_whole(tmp_path):
    """追記でない変更（戻した・書き換えた）は、手元に足さずクラウドの中身に揃える。"""
    gcs = FakeGcs(b"a\nb\n")
    f = synced(gcs, tmp_path)
    f.pull()
    gcs._set(b"short")  # 短くなった
    f.pull()
    assert f.local.read_bytes() == b"short"
    gcs._set(b"XXXXX and longer")  # 同じ長さの部分が書き換わって、長くなった
    f.pull()
    assert f.local.read_bytes() == b"XXXXX and longer"
    add(f, "mine\n")
    assert f.push()
    assert gcs.content == b"XXXXX and longermine\n"


def test_settings_file_always_matches_cloud(tmp_path):
    """中身が入れ替わるファイルは追記として扱わず、手元の変更も残さない（長さが同じでも）。"""
    gcs = FakeGcs(b'{"a": 1}', name="state/x.json")
    f = synced(gcs, tmp_path, append_only=False)
    f.pull()
    f.local.write_bytes(b'{"a": 9}')
    f.pull()
    assert f.local.read_bytes() == b'{"a": 1}'
    gcs._set(b'{"a": 22}')
    f.pull()
    assert f.local.read_bytes() == b'{"a": 22}'


def test_failed_push_is_dropped_by_next_pull_as_before(tmp_path):
    """pull は手元をクラウドに揃える（上げていない分は持ち越さない。これまでと同じ動き）。"""
    gcs = FakeGcs(b"a\n")
    f = synced(gcs, tmp_path)
    f.pull()
    add(f, "unpushed\n")
    f.pull()
    assert f.local.read_bytes() == b"a\n"


def test_push_creates_missing_file(tmp_path):
    gcs = FakeGcs(None)
    f = synced(gcs, tmp_path)
    f.pull()
    f.local.write_text("first\n")
    assert f.push()
    assert gcs.content == b"first\n"
    add(f, "second\n")
    assert f.push()
    assert gcs.content == b"first\nsecond\n"


def test_empty_cloud_file_then_append(tmp_path):
    gcs = FakeGcs(b"")
    f = synced(gcs, tmp_path)
    f.pull()
    add(f, "first\n")
    assert f.push()
    assert gcs.content == f.local.read_bytes() == b"first\n"


def test_pull_of_missing_object_leaves_no_empty_file(tmp_path):
    """クラウドに無いファイルを空で作らない（空の JSON を読んで AI が毎回失敗していた）。"""
    local = tmp_path / "ai_models.json"
    local.write_text("古い")
    f = SyncedFile(FakeGcs(None), "b", "state/ai_models.json", local)
    f.pull()
    assert not local.exists()
    f.pull()
    assert not local.exists()
