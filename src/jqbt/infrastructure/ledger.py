"""運用の台帳（SQLite、追記のみ）。

標準ライブラリだけで動き、バックアップはファイルのコピーで済む。
約定・シグナル・設定変更は **後から消せない**。間違えた記録は、訂正の行を足して直す。
（シグナルの状態だけは、planned → filled のように更新される）

プロファイルごとにファイルを分ける（``ops_paper.db`` / ``ops_live.db``）。
ペーパーの仮想約定が、実際の約定と混ざらないようにするため。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parents[3] / "state"

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, key TEXT, old TEXT, new TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT, symbol TEXT, side TEXT,
    quantity REAL, ref_price REAL, reason TEXT, version TEXT, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, date TEXT, symbol TEXT, side TEXT,
    quantity REAL, price REAL, fee REAL, source TEXT, ref_open REAL, note TEXT);
CREATE TABLE IF NOT EXISTS equity (date TEXT PRIMARY KEY, equity REAL, cash REAL);
"""


def path_for(profile: str) -> Path:
    return STATE_DIR / f"ops_{profile}.db"


def kill_path(profile: str) -> Path:
    """キルスイッチのファイル。在れば候補を出さない。CLI と GUI の両方が見る。"""
    return STATE_DIR / f"KILL_{profile}"


class Ledger:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # GUI（Streamlit）は処理ごとにスレッドが替わるので、同じ接続を使い回せるようにする。
        # 使うのは一人だけで、書き込みは都度コミットするので、これで足りる
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- 状態と監査 ---------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str, *, reason: str = "", at: str = "") -> None:
        old = self.get_state(key)
        with self._db:
            self._db.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.execute(
                "INSERT INTO audit(at, key, old, new, reason) VALUES(?, ?, ?, ?, ?)",
                (at, key, old, value, reason),
            )

    def audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- シグナル -----------------------------------------------------
    def add_signal(self, record: dict[str, Any]) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO signals(as_of, symbol, side, quantity, ref_price, reason, version, status, note) "
                "VALUES(:as_of, :symbol, :side, :quantity, :ref_price, :reason, :version, :status, :note)",
                {"reason": "", "version": "", "note": "", **record},
            )
        return int(cursor.lastrowid)

    def signals(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            rows = self._db.execute("SELECT * FROM signals ORDER BY id")
        else:
            rows = self._db.execute("SELECT * FROM signals WHERE status = ? ORDER BY id", (status,))
        return [dict(r) for r in rows]

    def set_signal_status(self, signal_id: int, status: str) -> None:
        with self._db:
            self._db.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))

    # --- 約定 ---------------------------------------------------------
    def add_fill(self, record: dict[str, Any]) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO fills(signal_id, date, symbol, side, quantity, price, fee, source, ref_open, note) "
                "VALUES(:signal_id, :date, :symbol, :side, :quantity, :price, :fee, :source, :ref_open, :note)",
                {"signal_id": None, "fee": 0.0, "source": "manual", "ref_open": None, "note": "", **record},
            )
        return int(cursor.lastrowid)

    def fills(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute("SELECT * FROM fills ORDER BY date, id")]

    # --- 資産曲線 -----------------------------------------------------
    def save_equity(self, date: str, equity: float, cash: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO equity(date, equity, cash) VALUES(?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET equity = excluded.equity, cash = excluded.cash",
                (date, equity, cash),
            )

    def equity_curve(self) -> list[tuple[str, float, float]]:
        rows = self._db.execute("SELECT date, equity, cash FROM equity ORDER BY date")
        return [(r["date"], r["equity"], r["cash"]) for r in rows]
