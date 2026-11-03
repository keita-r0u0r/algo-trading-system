"""前の版に戻す版を選ぶ。**読むだけ**（反映は make rollback が、確かめてから行う）。

  python scripts/fx_rollback.py            # 戻す先の候補と、その版で何が違うかを表示する
  python scripts/fx_rollback.py --pick     # 戻す先の版だけを出す（make rollback が使う）
  python scripts/fx_rollback.py --to 333b491

戻す先は、リリースログ（state/releases.jsonl）で反映の確認が通った（result = ok）版のうち、今の見張り役の版より前の、
いちばん新しいもの。戻すと、その版のあとに入った変更（下に表示する PR）が無くなる。

戻す前に確かめること（docs/release.md）:
  * 判断する者の段階（画面の「売買の作り」）を切り替えるだけで済まないか。済むなら、反映し直さない
  * 戻す版が知らない判断する者の建玉が無いか（あれば、戻したあとは誰も決済しない。GMOコイン側の損切りだけが効く）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fx_verify import reader  # noqa: E402


def orphaned_positions(read, version: str) -> list[str]:
    """今の実際の建玉のうち、``version`` の版の判断する者の一覧（LiveConfig.accounts）に無いもの。"""
    import subprocess

    from jqbt.application.fx_live_trade import book_from_events

    source = subprocess.run(["git", "show", f"{version}:src/jqbt/application/fx_live_trade.py"],
                            capture_output=True, text=True, check=False).stdout
    if not source:
        return [f"（版 {version} の中身を git で読めなかったので確かめられない）"]
    events = [json.loads(x) for x in read("state/fx_live.jsonl").decode().splitlines() if x.strip()]
    return [f"{p.account}: {p.symbol} {p.side} {p.size}通貨" for p in book_from_events(events).values()
            if f'"{p.account}"' not in source]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pick", action="store_true", help="戻す先の版だけを出す")
    parser.add_argument("--to", help="戻す先の版を指定する（反映の確認が通った版だけ）")
    args = parser.parse_args()
    read, _, _ = reader()
    releases = [json.loads(x) for x in read("state/releases.jsonl").decode().splitlines() if x.strip()]
    watch = json.loads(read("state/fx_watch.json") or b"{}")
    current = watch.get("version")
    ok = [r for r in releases if r.get("result") in ("ok", "backfill")]
    versions = [r["version"] for r in ok]
    if args.to:
        if args.to not in versions:
            print(f"版 {args.to} は、反映の確認が通った版にありません（{', '.join(versions)}）", file=sys.stderr)
            return 1
        target = args.to
    else:
        before = versions[:versions.index(current)] if current in versions else versions
        before = [v for v in before if v != current]
        if not before:
            print("戻せる版がありません（反映の確認が通った、今より前の版が無い）", file=sys.stderr)
            return 1
        target = before[-1]
    if args.pick:
        print(target)
        return 0
    lost = []
    seen = False
    for r in releases:
        if seen and r.get("version") != target:
            lost += r.get("prs") or []
        if r.get("version") == target:
            seen = True
    print(f"今の版: {current}\n戻す先: {target}\n戻すと無くなる変更（PR）: {', '.join(f'#{n}' for n in sorted(set(lost))) or 'なし'}")
    orphans = orphaned_positions(read, target)
    if orphans:
        print("⚠ 戻す先の版が知らない判断する者の建玉があります（戻すと誰も決済しない。GMOコイン側の損切りだけが効く）:")
        for line in orphans:
            print("   " + line)
        print("  先に、画面でその判断する者を「止める」にして決済を待つか、GMOコインのアプリで決済することを考える")
    print("戻すなら: make rollback（指定するなら make rollback TO=<版>）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
