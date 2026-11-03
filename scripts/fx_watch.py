"""見張り役（常時動く）。実際の売買の **随時判断** と **定期判断** を、この中で行う。

  python scripts/fx_watch.py             # 見張って、実際の売買もする（GCP では仮想マシンの上で動き続ける）
  python scripts/fx_watch.py --dry-run   # 見張るだけ（起こす理由を表示する。判断も発注もしない）

  * GMOコインの公開の値段の配信（WebSocket）を受け取り続け、値段が届くたびに条件（application/fx_watch.py）を確かめる
  * 随時判断: 急変・建玉が損切り/利確に近い・時間帯の切り替わりのとき、**その場で** 判断して発注する（AI も呼ぶ）
  * 定期判断: 5分ごとに、実際の口座の判断をする（AI は15分ごと）
  * 判断と発注の段取りは、ジョブと同じ部品（interfaces/fx_runtime.py・fx_live.py）を使う
  * 記録（state/ の一部）は Cloud Storage と同期する。追記した分だけを、他の書き込みを上書きせずに上げる
  * 1分ごとに生きている印を state/fx_watch.json に書く。口座に届いた回が新しい間、ジョブは実際の売買をしない（ペーパーだけ）

環境変数: GCP_PROJECT, STATE_BUCKET, FX_WATCH_TRADES（true で実際の売買もする）, FX_LIVE_ENABLED ほか
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jqbt.application.fx_watch import PriceHistory, WatchConfig, reason_to_wake, watch_snapshot  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
WS_URL = "wss://forex-api.coin.z.com/ws/public/v1"
SYMBOLS = ("USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY")
#: 見張り役は実際の売買の唯一の担い手なので、定期の回をよける必要はない（ジョブはペーパーだけ）
CONFIG = WatchConfig(scheduled_guard_minutes=0)
PERIODIC_SECONDS = 5 * 60


class Cloud:
    """Cloud Storage との同期（サービスアカウントの権限。仮想マシンのメタデータからトークンを取る）。"""

    def __init__(self, state_dir: Path) -> None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        from jqbt.infrastructure.gcs_sync import SyncedFile, SyncedFlag

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self.http = AuthorizedSession(credentials)
        bucket = os.environ["STATE_BUCKET"]
        self.bucket = bucket
        self.files = [SyncedFile(self.http, bucket, f"state/{n}", state_dir / n)
                      for n in ("fx_live.jsonl", "ai_usage.jsonl")]
        self.read_only = [SyncedFile(self.http, bucket, f"state/{n}", state_dir / n)
                          for n in ("ai_settings.json", "ai_models.json", "fx_positions.json")]
        self.flags = [SyncedFlag(self.http, bucket, "state/KILL_fx_live", state_dir / "KILL_fx_live")]

    def pull(self) -> None:
        for f in [*self.files, *self.read_only, *self.flags]:
            f.pull()

    def push(self) -> None:
        for f in self.files:
            f.push()
        for f in self.flags:
            f.push()

    def write_json(self, name: str, data: dict) -> None:
        self.http.post(f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
                       params={"uploadType": "media", "name": name},
                       data=json.dumps(data, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"},
                       timeout=20)


def current_positions(state_dir: Path) -> list[dict]:
    """見張る建玉: ペーパーの建玉（ジョブが書く要約）と、実際の建玉（この見張り役の記録）。"""
    from jqbt.application.fx_live_trade import book_from_events
    from jqbt.infrastructure import fx_live_log

    try:
        summary = json.loads((state_dir / "fx_positions.json").read_text(encoding="utf-8")).get("positions", [])
    except (OSError, ValueError):
        summary = []
    paper = [p for p in summary if p.get("venue") == "paper"]
    live = [{"venue": "live", "account": p.account, "symbol": p.symbol, "stop": p.stop, "take": p.take}
            for p in book_from_events(fx_live_log.load()).values()]
    return paper + live


def live_cycle(cloud: Cloud | None, reason: str) -> tuple[str, bool]:
    """実際の口座で1回判断して、必要なら発注する（ジョブと同じ部品）。(結果, 口座に届いたか)"""
    from fx_paper import build_ai

    from jqbt.interfaces import fx_runtime as rt

    if cloud:
        cloud.pull()
    ctx = rt.build_context(build_ai(), trigger=reason)
    if ctx is None:
        result, ok = "市場が閉まっている", True
    else:
        ok = rt.trade_live(ctx)
        rt.sync_live()
        result = "判断した" if ok else "口座に届かない（キー・IP の制限）"
    if cloud:
        cloud.push()
    return result, ok


async def watch(dry_run: bool) -> None:
    import websockets

    from jqbt.interfaces import fx_runtime as rt

    trades = os.environ.get("FX_WATCH_TRADES", "").lower() == "true" and not dry_run
    cloud = None if dry_run else Cloud(rt.STATE)
    history = PriceHistory(dt.timedelta(minutes=CONFIG.window_minutes))
    state = {"started_at": dt.datetime.now(JST).isoformat(timespec="seconds"), "last_tick": None,
             "trading": trades, "version": os.environ.get("JQBT_VERSION", ""),
             "last_cycle": None, "last_ok_cycle": None, "cycles": 0, "reconnects": 0, "wakes": []}
    positions: list[dict] = []
    last_wake: dt.datetime | None = None
    fired: set[str] = set()
    last_beat = last_check = last_positions = 0.0
    next_periodic = time.monotonic()
    busy = False
    if cloud:  # 起動したらすぐ「これから担当する」印を書く（ジョブは起動5分以内の見張り役に売買を譲る）
        await asyncio.to_thread(cloud.write_json, "state/fx_watch.json", state)

    async def cycle(reason: str) -> None:
        nonlocal busy
        busy = True
        started = time.monotonic()
        ok = False
        try:
            result, ok = await asyncio.to_thread(live_cycle, cloud, reason)
        except Exception as exc:  # noqa: BLE001  1回の失敗で見張りは止めない
            result = f"失敗: {type(exc).__name__}: {str(exc)[:160]}"
        finally:
            busy = False
        now = dt.datetime.now(JST)
        state["last_cycle"] = now.isoformat(timespec="seconds")
        if ok:
            state["last_ok_cycle"] = state["last_cycle"]
        state["cycles"] += 1
        if reason:
            state["wakes"] = (state["wakes"] + [{"at": now.isoformat(timespec="seconds"), "reason": reason,
                                                 "result": f"{result}（{time.monotonic() - started:.0f}秒）"}])[-30:]
        print(f"{now:%H:%M:%S} {'随時' if reason else '定期'}判断 {reason} → {result}", flush=True)
        if cloud:  # 回が終わったら、すぐ生きている印を書く（ジョブが古い印を見て、二重に売買しないように）
            await asyncio.to_thread(cloud.write_json, "state/fx_watch.json", state)

    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None) as ws:
                for symbol in SYMBOLS:
                    await ws.send(json.dumps({"command": "subscribe", "channel": "ticker", "symbol": symbol}))
                    await asyncio.sleep(1.1)  # 購読は1秒に1回まで
                async for message in ws:
                    tick = json.loads(message)
                    if "symbol" not in tick or "bid" not in tick:
                        continue
                    now = dt.datetime.now(JST)
                    history.add(tick["symbol"], now, (float(tick["bid"]) + float(tick["ask"])) / 2)
                    state["last_tick"] = now.isoformat(timespec="seconds")
                    clock = time.monotonic()
                    if cloud and clock - last_positions >= 60 and not busy:
                        last_positions = clock
                        positions = current_positions(rt.STATE)
                    if clock - last_check >= 1 and not busy:  # 条件は1秒ごと（値段は1秒に約10回届く）
                        last_check = clock
                        reason = reason_to_wake(now, history, positions, CONFIG, last_wake=last_wake, fired_sessions=fired)
                        if reason:
                            last_wake = now
                            if trades:
                                asyncio.create_task(cycle(reason))
                            else:
                                print(f"{now:%H:%M:%S} {reason} → （売買を担当していないので記録だけ）", flush=True)
                        elif trades and clock >= next_periodic:
                            next_periodic = clock + PERIODIC_SECONDS
                            asyncio.create_task(cycle(""))
                    if cloud and clock - last_beat >= 60:
                        last_beat = clock
                        state["watch"] = watch_snapshot(now, history, positions, CONFIG, last_wake=last_wake,
                                                        fired_sessions=fired)
                        state["next_periodic_at"] = (now + dt.timedelta(seconds=max(0.0, next_periodic - clock))
                                                     ).isoformat(timespec="seconds") if trades else None
                        await asyncio.to_thread(cloud.write_json, "state/fx_watch.json", state)
        except Exception as exc:  # 切れたら、少し待ってつなぎ直す
            state["reconnects"] += 1
            print(f"{dt.datetime.now(JST):%H:%M:%S} 配信が切れました（{type(exc).__name__}: {str(exc)[:120]}）。つなぎ直します",
                  flush=True)
            await asyncio.sleep(min(60, 5 * state["reconnects"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from jqbt.infrastructure.env import load_env

    load_env()
    asyncio.run(watch(args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
