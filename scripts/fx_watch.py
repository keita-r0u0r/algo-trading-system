"""値段の見張り役（常時動く）。機械的な条件に当たったときだけ、判断の回（Cloud Run のジョブ）を起こす。

  python scripts/fx_watch.py             # 見張る（GCP では仮想マシンの上で動き続ける）
  python scripts/fx_watch.py --dry-run   # 起こさずに、起こす理由を表示するだけ

  * GMOコインの公開の値段の配信（WebSocket）を受け取り続ける。注文用のキーは持たない（値段を見るだけ）
  * 条件（application/fx_watch.py）: 急変・建玉が損切り/利確に近い・時間帯の切り替わり
  * 起こすときは、ジョブに「起こした理由」を渡す（AI はそれを見て判断する）
  * 1分ごとに、生きている印（最後に値段を受け取った時刻・起こした記録）を state/fx_watch.json に書く（画面で見る）
  * 建玉は、ジョブが書く state/fx_positions.json から読む（毎分）

環境変数: GCP_PROJECT, STATE_BUCKET, FX_JOB_NAME, FX_JOB_REGION
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

from jqbt.application.fx_watch import PriceHistory, WatchConfig, reason_to_wake  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
WS_URL = "wss://forex-api.coin.z.com/ws/public/v1"
SYMBOLS = ("USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY")
STATE_OBJECT, POSITIONS_OBJECT = "state/fx_watch.json", "state/fx_positions.json"


class Cloud:
    """GCS と Cloud Run を、サービスアカウントの権限で呼ぶ（仮想マシンのメタデータからトークンを取る）。"""

    def __init__(self) -> None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self.http = AuthorizedSession(credentials)
        self.project = os.environ["GCP_PROJECT"]
        self.bucket = os.environ["STATE_BUCKET"]
        self.job = os.environ.get("FX_JOB_NAME", "algo-trading-fx-paper")
        self.region = os.environ.get("FX_JOB_REGION", "asia-northeast1")

    def read_json(self, name: str) -> dict:
        from urllib.parse import quote

        r = self.http.get(f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}",
                          params={"alt": "media"}, timeout=20)
        return r.json() if r.status_code == 200 else {}

    def write_json(self, name: str, data: dict) -> None:
        self.http.post(f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
                       params={"uploadType": "media", "name": name},
                       data=json.dumps(data, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"},
                       timeout=20)

    def wake(self, reason: str) -> str:
        url = f"https://run.googleapis.com/v2/projects/{self.project}/locations/{self.region}/jobs/{self.job}:run"
        body = {"overrides": {"containerOverrides": [{"env": [{"name": "TRIGGER_REASON", "value": reason[:200]}]}]}}
        r = self.http.post(url, json=body, timeout=30)
        return "起こした" if r.status_code < 300 else f"起こせなかった（HTTP {r.status_code}: {r.text[:120]}）"


async def watch(dry_run: bool) -> None:
    import websockets

    config = WatchConfig()
    history = PriceHistory(dt.timedelta(minutes=config.window_minutes))
    cloud = None if dry_run else Cloud()
    state = {"started_at": dt.datetime.now(JST).isoformat(timespec="seconds"), "last_tick": None,
             "reconnects": 0, "wakes": []}
    positions: list[dict] = []
    last_wake: dt.datetime | None = None
    fired: set[str] = set()
    last_beat = last_check = 0.0

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
                    if time.monotonic() - last_check >= 30:
                        last_check = time.monotonic()
                        if cloud and time.monotonic() - last_beat >= 60:
                            positions = cloud.read_json(POSITIONS_OBJECT).get("positions", [])
                        reason = reason_to_wake(now, history, positions, config, last_wake=last_wake,
                                                fired_sessions=fired)
                        if reason:
                            last_wake = now
                            result = "（試しに動かしているので起こさない）" if dry_run else cloud.wake(reason)
                            print(f"{now:%H:%M:%S} {reason} → {result}", flush=True)
                            state["wakes"] = (state["wakes"] + [{"at": now.isoformat(timespec="seconds"),
                                                                 "reason": reason, "result": result}])[-30:]
                    if cloud and time.monotonic() - last_beat >= 60:
                        last_beat = time.monotonic()
                        cloud.write_json(STATE_OBJECT, state)
        except Exception as exc:  # 切れたら、少し待ってつなぎ直す
            state["reconnects"] += 1
            print(f"{dt.datetime.now(JST):%H:%M:%S} 配信が切れました（{type(exc).__name__}: {str(exc)[:120]}）。つなぎ直します",
                  flush=True)
            await asyncio.sleep(min(60, 5 * state["reconnects"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(watch(args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
