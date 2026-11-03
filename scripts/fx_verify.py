"""反映したあとの確認。新しい版の見張り役が、本当に判断して実際の口座に届いているかを確かめる。**読むだけ。**

  python scripts/fx_verify.py --version 4526b44          # 新しい版が動くまで待って確かめる（既定で最大25分）
  python scripts/fx_verify.py                            # 今の状態の健康診断だけ

確かめること（どれか1つでも満たせなければ、失敗で終わる）:
  * 見張り役の生きている印が、指定した版のもので、起動後の判断の回が実際の口座に届いている
  * 起動後に、AI が実際に判断している（呼べなかったら、その理由を出してすぐ失敗）
  * 起動後に、ロジック（金利差＋流れ）が判断している
  * 健康診断（application/fx_health.py）に異常（bad）が無い

記録は Cloud Storage（STATE_BUCKET、無ければ GCP_PROJECT から決める）から読む。市場が閉まっている間は確かめられない。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.application.fx_health import check_all, market_open  # noqa: E402
from jqbt.application.fx_live_trade import LiveConfig  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))


def reader():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    http = AuthorizedSession(credentials)
    bucket = os.environ.get("STATE_BUCKET") or f"{os.environ.get('GCP_PROJECT') or project}-algo-trading-state"

    def read(name: str) -> bytes:
        r = http.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name.replace('/', '%2F')}",
                     params={"alt": "media"}, timeout=30)
        return b"" if r.status_code == 404 else (r.raise_for_status() or r.content)

    def exists(name: str) -> bool:
        r = http.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name.replace('/', '%2F')}", timeout=30)
        return r.status_code == 200

    return read, exists


def after(events: list[dict], kind: str, account: str, since: str) -> list[dict]:
    """``since`` より後に **記録された** 出来事。判断の時刻（at）は分で切り捨てなので、記録した時刻（logged_at）で比べる。"""
    start = dt.datetime.fromisoformat(since) if since else None

    def logged(e: dict) -> dt.datetime | None:
        try:
            return dt.datetime.fromisoformat(str(e.get("logged_at") or e.get("at")))
        except ValueError:
            return None

    return [e for e in events if e.get("kind") == kind and e.get("account") == account
            and (start is None or ((t := logged(e)) is not None and t >= start))]


def check_once(read, exists, version: str | None) -> tuple[str, list[str]]:
    """('ok' | 'wait' | 'fail', 説明)。"""
    now = dt.datetime.now(JST)
    watch = json.loads(read("state/fx_watch.json") or b"{}")
    events = [json.loads(line) for line in read("state/fx_live.jsonl").decode().splitlines() if line.strip()]
    findings = check_all(now, watch=watch, live_events=events, accounts=LiveConfig().accounts,
                         live_enabled=True, killed=exists("state/KILL_fx_live"),
                         paper_paused=exists("state/PAUSE_fx_paper"))
    lines = [f"{f.state:>4}  {f.name}: {f.text}" for f in findings]
    if version:
        if watch.get("version") != version:
            return "wait", [f"見張り役はまだ版 {watch.get('version') or '?'}（{version} の起動を待っている）"]
        started = str(watch.get("started_at") or "")
        if not (watch.get("last_ok_cycle") and watch["last_ok_cycle"] >= started):
            return "wait", [f"版 {version} の見張り役が起動した（{started[11:19]}）。最初の判断の回を待っている"]
        ai = after(events, "live_decision", "ai", started)
        failed = [e for e in ai if e.get("error")]
        if failed:
            return "fail", [f"AI を呼べなかった: {failed[-1]['error']}", *lines]
        if not any(e.get("model") for e in ai):
            return "wait", [f"AI が実際に判断するのを待っている（起動後の回 {len(ai)} 回）"]
        rule = [e for name in LiveConfig().accounts if name != "ai"
                for e in after(events, "live_decision", name, started)]
        if not any(e.get("logic") for e in rule):
            return "wait", ["ロジックが判断するのを待っている"]
    if any(f.state == "bad" for f in findings):
        return "fail", lines
    return "ok", lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", help="反映した版（見張り役の JQBT_VERSION）。省くと今の健康診断だけ")
    parser.add_argument("--wait", type=int, default=25 * 60, help="待つ最大の秒数")
    args = parser.parse_args()
    if not market_open(dt.datetime.now(JST)):
        print("市場が閉まっているので、確かめられません（土曜 7:00〜月曜 7:00）")
        return 0
    read, exists = reader()
    deadline = time.monotonic() + (args.wait if args.version else 0)
    last = None
    while True:
        state, lines = check_once(read, exists, args.version)
        if state != "wait" or time.monotonic() >= deadline:
            break
        if lines != last:
            print(f"{dt.datetime.now(JST):%H:%M:%S} " + lines[0], flush=True)
            last = lines
        time.sleep(20)
    verdict = {"ok": "✅ 確認できました", "fail": "❌ 失敗", "wait": "❌ 時間切れ（確認できなかった）"}[state]
    print(verdict)
    for line in lines:
        print("  " + line)
    return 0 if state == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
