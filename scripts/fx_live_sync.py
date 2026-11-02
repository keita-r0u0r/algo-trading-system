"""GMOコインの実際の約定・建玉・資産を読み、記録する。**読むだけ。注文は出さない。**

  python scripts/fx_live_sync.py

約定の履歴は API の都合で直近1日分しか取れないので、1日1回以上動かす（Cloud Run ではペーパー運用と一緒に15分ごと）。
記録は ``state/fx_live.jsonl``。APIキーに照会の権限が足りないものは、読めなかったことを記録して進める。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from jqbt.application.fx_live import new_executions, slippage_pips  # noqa: E402
from jqbt.infrastructure import fx_live_log  # noqa: E402
from jqbt.infrastructure.env import load_env  # noqa: E402
from jqbt.infrastructure.gmo_fx import GmoError, GmoFxClient  # noqa: E402
from jqbt.infrastructure.gmo_fx_store import fetch_day  # noqa: E402
from jqbt.infrastructure.secrets import SecretError, build_secrets  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
SYMBOLS = ("USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY")


def quote_at(api: GmoFxClient, symbol: str, at: dt.datetime, cache: dict) -> tuple[float, float] | None:
    """約定した1分足の始まりの売値・買値。GMO の営業日（朝6時または7時に始まる）で足を取る。"""
    minute = pd.Timestamp(at.astimezone(JST).replace(tzinfo=None)).floor("min")
    for day in (minute.date(), (minute - pd.Timedelta(days=1)).date()):
        key = (symbol, day)
        if key not in cache:
            try:
                cache[key] = fetch_day(api, symbol, day)
            except GmoError:
                cache[key] = pd.DataFrame()
        bars = cache[key]
        if not bars.empty and minute in bars.index:
            row = bars.loc[minute]
            return float(row["bid_open"]), float(row["ask_open"])
    return None


def sync(path: Path = fx_live_log.DEFAULT_PATH, quiet: bool = False) -> dict:
    """1回読む。返すのは、新しく記録した約定の数と、読めなかったもの。"""
    load_env()
    try:
        secrets = build_secrets()
        key, secret = secrets.get("gmo_fx_api_key"), secrets.get("gmo_fx_api_secret")
    except SecretError as exc:
        return {"new": 0, "errors": [f"キーを読めません: {exc}"]}
    if not (key and secret):
        return {"new": 0, "errors": ["GMOコインのキーがありません"]}
    api = GmoFxClient(key, secret)
    now = dt.datetime.now(JST).isoformat(timespec="seconds")
    events, errors, cache = [], [], {}
    known = fx_live_log.known_execution_ids(fx_live_log.load(path))

    for symbol in SYMBOLS:
        try:
            fetched = api.latest_executions(symbol)
        except GmoError as exc:
            errors.append(f"約定（{symbol}）: {exc}")
            break  # 権限が無いなら、どの通貨ペアでも同じ
        for e in new_executions(fetched, known):
            at = dt.datetime.fromisoformat(str(e["timestamp"]).replace("Z", "+00:00"))
            quote = quote_at(api, symbol, at, cache)
            slip = slippage_pips(e["side"], float(e["price"]), *quote) if quote else None
            events.append({"kind": "execution", "at": at.astimezone(JST).isoformat(timespec="seconds"),
                           **e, "bid_at": quote[0] if quote else None, "ask_at": quote[1] if quote else None,
                           "slippage_pips": slip})
    snapshot = {"kind": "snapshot", "at": now}
    for name, call in (("assets", api.assets), ("positions", api.open_positions), ("summary", api.position_summary)):
        try:
            snapshot[name] = call()
        except GmoError as exc:
            errors.append(f"{name}: {exc}")
    events.append(snapshot)
    if errors:
        events.append({"kind": "error", "at": now, "errors": errors})
    fx_live_log.append(events, path)
    result = {"new": sum(e["kind"] == "execution" for e in events), "errors": errors}
    if not quiet:
        print(f"実際の約定: 新しく {result['new']} 件" + (f" / 読めなかったもの: {'; '.join(errors)}" if errors else ""))
    return result


if __name__ == "__main__":
    sync()
