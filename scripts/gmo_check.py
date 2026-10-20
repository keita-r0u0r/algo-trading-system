"""GMOコイン 外国為替FX との疎通確認。読み取りだけで、注文は出さない。

  python scripts/gmo_check.py              # 公開の情報と、自分の口座の資産（キーがあれば）
  python scripts/gmo_check.py --public     # キーを使わない

キーは GCP Secret Manager の gmo-fx-api-key / gmo-fx-api-secret から読む（docs/secrets.md）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.infrastructure.env import load_env  # noqa: E402
from jqbt.infrastructure.gmo_fx import GmoError, GmoFxClient  # noqa: E402
from jqbt.infrastructure.secrets import SecretError, build_secrets  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--public", action="store_true", help="キーを使わず、公開の情報だけ確かめる")
    parser.add_argument("--symbol", default="USD_JPY")
    args = parser.parse_args()
    load_env()

    api = GmoFxClient()
    print(f"市場の状態: {api.status()}")

    spec = next((s for s in api.symbols() if s["symbol"] == args.symbol), None)
    if spec:
        print(f"{args.symbol}: 最小 {spec['minOpenOrderSize']} 通貨 / 刻み {spec['sizeStep']} / "
              f"最大 {spec['maxOrderSize']} / 価格の刻み {spec['tickSize']}")
    tick = next((t for t in api.ticker() if t["symbol"] == args.symbol), None)
    if tick:
        bid, ask = float(tick["bid"]), float(tick["ask"])
        print(f"{args.symbol}: 売 {bid} / 買 {ask} / スプレッド {(ask - bid) * 100:.1f} 銭 ({tick['status']})")
        if spec:
            size = float(spec["minOpenOrderSize"])
            print(f"  最小の建玉 {size:,.0f} 通貨 = 約 {size * ask:,.0f} 円分（必要証拠金はその 1/25 = 約 {size * ask / 25:,.0f} 円）")

    # 直近の営業日の1分足（週末は空なので、さかのぼる）
    day = dt.datetime.now(JST).date()
    for _ in range(7):
        bars = api.klines(args.symbol, "1min", day.strftime("%Y%m%d"))
        if bars:
            first = dt.datetime.fromtimestamp(int(bars[0]["openTime"]) / 1000, JST)
            last = dt.datetime.fromtimestamp(int(bars[-1]["openTime"]) / 1000, JST)
            print(f"1分足 {day}: {len(bars)} 本（{first:%H:%M} 〜 {last:%m-%d %H:%M}）")
            break
        day -= dt.timedelta(days=1)

    if args.public:
        return 0
    try:
        secrets = build_secrets()
        key, secret = secrets.get("gmo_fx_api_key"), secrets.get("gmo_fx_api_secret")
    except SecretError as exc:
        print(f"キーを読めません: {exc}")
        return 1
    if not (key and secret):
        print(f"キーがまだありません（{secrets.describe()} の gmo-fx-api-key / gmo-fx-api-secret）。docs/secrets.md")
        return 1
    try:
        assets = GmoFxClient(key, secret).assets()
    except GmoError as exc:
        print(f"口座の照会に失敗しました: {exc}")
        return 1
    rows = assets if isinstance(assets, list) else [assets]
    for a in rows:
        print(f"口座: 評価額 {a.get('equity')} 円 / 取引余力 {a.get('availableAmount')} 円 / "
              f"証拠金維持率 {a.get('marginRatio') or '-'}")
    print("疎通 OK（照会だけ。注文は出していません）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
