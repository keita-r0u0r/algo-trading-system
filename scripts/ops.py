"""運用の入口。売買候補を出し、約定を記録し、想定とのずれを測る。

**注文は出しません。** 発注は人が証券会社のアプリで行い、約定をここに記録します。
出力は検証ツールの結果であって、投資助言ではありません。結果は保証されません。

  # ペーパー（仮想の約定）で流れを確かめる
  python scripts/ops.py --profile paper init --cash 10000
  python scripts/ops.py --profile paper replay --from 2025-07-01 --to 2026-02-18
  python scripts/ops.py --profile paper status

  # 実運用（新しいデータが要ります。Free プランは12週間遅れで、候補は出ません）
  python scripts/ops.py --profile live init --cash 10000
  python scripts/ops.py --profile live plan
  python scripts/ops.py --profile live fill --symbol 2012 --side buy --qty 4 --price 246
  python scripts/ops.py --profile live status

  # すべて止める / 再開する
  python scripts/ops.py --profile live kill --reason "想定外の動き"
  python scripts/ops.py --profile live resume --reason "原因を確認した"
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.application.operations import OperationsError, OperationsService, StaleDataError
from jqbt.core.gates import Mode
from jqbt.core.risk import RiskLimits
from jqbt.infrastructure.env import load_env
from jqbt.infrastructure.ledger import Ledger, kill_path, path_for
from jqbt.interfaces.composition import DataRequest, load_panel
from jqbt.strategies import REGISTRY

load_env()

DISCLAIMER = "※ 検証ツールの出力であり、投資助言ではありません。発注は人が行います。結果は保証されません。"
ROOT = Path(__file__).resolve().parents[1]


def kill_file(profile: str) -> Path:
    return kill_path(profile)


def git_version() -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--", "src", "scripts"],
                cwd=ROOT, capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return "unknown", False


def parse_params(items: list[str]) -> dict:
    params: dict = {}
    for item in items:
        key, _, raw = item.partition("=")
        for cast in (int, float):
            try:
                params[key] = cast(raw)
                break
            except ValueError:
                continue
        else:
            params[key] = raw
    return params


def build_service(args) -> OperationsService:
    panel = load_panel(DataRequest(synthetic=False, start=None, end=None, symbols=None, regime="mixed"))
    ledger = Ledger(path_for(args.profile))

    def factory(params):
        name = ledger.get_state("strategy") or args.strategy
        return REGISTRY[name].factory(params)

    return OperationsService(ledger, panel, factory, lot_size=args.lot_size)


def latest(service: OperationsService) -> dt.date:
    return service.index.dates[-1]


def money(value: float) -> str:
    return f"{value:,.0f}円"


# ----------------------------------------------------------------------
def cmd_init(args, service: OperationsService) -> int:
    if args.strategy not in REGISTRY:
        raise OperationsError(f"未知の戦略: {args.strategy}（{', '.join(sorted(REGISTRY))}）")
    params = {"max_positions": 5, **parse_params(args.param)}
    allowed = set(REGISTRY[args.strategy].grid) | {"max_positions"}
    unknown = set(params) - allowed
    if unknown:
        # 凍結する内容が実際の動作と食い違わないよう、使われない設定は受け付けない
        raise OperationsError(f"{args.strategy} に無いパラメータ: {sorted(unknown)}（使えるのは {sorted(allowed)}）")
    commit, dirty = git_version()
    mode = Mode.PAPER if args.profile == "paper" else Mode.SIGNAL
    now = dt.datetime.now()
    service.init(
        cash=args.cash, strategy=args.strategy, params=params,
        version=f"{commit}{'+未コミットの変更あり' if dirty else ''}",
        limits=RiskLimits(), mode=mode, started_on=dt.date.today(), at=now.isoformat(timespec="seconds"),
    )
    print(f"初期化しました（{args.profile}）。戦略 {args.strategy} {params} を凍結。モード: {mode.name}")
    if dirty:
        print("! 未コミットの変更があります。凍結した版を再現できなくなります。コミットしてからの初期化を勧めます")
    return 0


def cmd_plan(args, service: OperationsService) -> int:
    if kill_file(args.profile).exists():
        raise OperationsError("キルスイッチが入っています。resume するまで候補を出しません")
    as_of = args.as_of or latest(service)
    service.apply_pending(dt.datetime.now())
    service.mark(as_of)
    frozen = (service.ledger.get_state("version") or "").split("+")[0]
    commit, _ = git_version()
    if frozen and frozen != commit:
        print(f"! 凍結した版（{frozen}）と現在のコード（{commit}）が違います。結果は別の戦略の出力かもしれません\n")
    result = service.plan(as_of, today=dt.date.today())
    print(f"【売買候補】データの最新日 {as_of} の引け後。執行は翌営業日の寄り")
    if result.already:
        print("（この日の候補は作成済みです）")
    if not result.signals:
        print("  候補はありません" + ("" if result.due else "（今日は入れ替え日ではありません）"))
    for s in result.signals:
        print(f"  #{s['id']:<4}{s['side']:<5}{s['symbol']:<7}{s['quantity']:>7g}株  参考 {s['ref_price']:,.1f}円  {s['reason']}")
    for text, why in result.rejected:
        print(f"  見送り {text}: {why}")
    print(f"\n{DISCLAIMER}")
    return 0


def cmd_fill(args, service: OperationsService) -> int:
    day = args.date or dt.date.today()
    record = service.record_fill(
        day=day, symbol=args.symbol, side=args.side, quantity=args.qty, price=args.price,
        fee=args.fee, signal_id=args.signal_id, note=args.note or "",
    )
    print(f"記録しました: {day} {args.side} {args.symbol} {args.qty:g}株 @ {args.price:,.1f}円")
    if record["ref_open"]:
        diff = args.price / record["ref_open"] - 1
        diff = diff if args.side == "buy" else -diff
        print(f"  始値 {record['ref_open']:,.1f}円との差: {diff:+.2%}（不利な向きが正）")
    else:
        print("  この日の始値はデータに無く、ずれは測れません（データが追いついたら再計測できます）")
    return 0


def cmd_mark(args, service: OperationsService) -> int:
    day = args.date or latest(service)
    equity, cash = service.mark(day)
    print(f"{day} 評価額 {money(equity)}（現金 {money(cash)}）")
    return 0


def cmd_status(args, service: OperationsService) -> int:
    day = args.date or latest(service)
    service.apply_pending(dt.datetime.now())
    st = service.status(day)
    print(f"プロファイル {args.profile} / モード {st['mode'].name} / 戦略 {st['strategy']} {st['params']}")
    print(f"凍結した版 {st['version']} / 最後の入れ替え {st['last_rebalance'] or '—'}")
    if kill_file(args.profile).exists():
        print("! キルスイッチが入っています")
    print(f"\n評価額 {money(st['equity'])} / 現金 {money(st['cash'])}（{day} 時点）")
    for p in st["positions"]:
        print(f"  {p['symbol']:<7}{p['quantity']:>7g}株  {p['price']:>9,.1f}円  {money(p['value'])}")
    lim = st["limits"]
    print(f"\nリスク制限: 1注文 {money(lim.max_order_value)} / 1銘柄 評価額の{lim.max_position_share:.0%} / "
          f"{lim.max_positions}銘柄 / 日次損失 {lim.max_daily_loss:.0%}")
    if st["pending_limits"]:
        print("  緩める変更が待機中です（24時間後に有効）")
    print("\n【増額の条件】全部満たすまで、資金を増やさない")
    for c in st["gates"]:
        print(f"  [{'OK' if c.ok else '未'}] {c.name}: {c.detail}")
    print("  → " + ("全て満たしています" if st["gates_passed"] else "まだ満たしていません"))
    if st["planned"]:
        print(f"\n未約定の候補 {len(st['planned'])}件: " + ", ".join(f"#{s['id']} {s['side']} {s['symbol']}" for s in st["planned"]))
    return 0


def cmd_set_mode(args, service: OperationsService) -> int:
    service.set_mode(Mode(args.mode), reason=args.reason, at=dt.datetime.now().isoformat(timespec="seconds"))
    print(f"モードを {Mode(args.mode).name} にしました")
    return 0


def cmd_set_limit(args, service: OperationsService) -> int:
    current = asdict(service.limits)
    changes = parse_params(args.item)
    unknown = set(changes) - set(current)
    if unknown:
        raise OperationsError(f"未知の項目: {sorted(unknown)}（{sorted(current)}）")
    new = replace(service.limits, **changes)
    outcome = service.request_limits(new, reason=args.reason, now=dt.datetime.now())
    print("厳しくする変更なので、すぐ適用しました" if outcome == "applied"
          else "緩める変更を含むため、24時間後に有効になります（それまでは現在の制限のまま）")
    return 0


def cmd_kill(args, service: OperationsService) -> int:
    kill_file(args.profile).parent.mkdir(parents=True, exist_ok=True)
    kill_file(args.profile).write_text(args.reason, encoding="utf-8")
    service.set_mode(Mode.STOP, reason=f"キルスイッチ: {args.reason}", at=dt.datetime.now().isoformat(timespec="seconds"))
    print("停止しました。候補は出ません。記録（約定の入力）はできます")
    return 0


def cmd_resume(args, service: OperationsService) -> int:
    kill_file(args.profile).unlink(missing_ok=True)
    mode = Mode.PAPER if args.profile == "paper" else Mode.SIGNAL
    service.set_mode(mode, reason=f"再開: {args.reason}", at=dt.datetime.now().isoformat(timespec="seconds"))
    print(f"再開しました。モード: {mode.name}")
    return 0


def cmd_replay(args, service: OperationsService) -> int:
    start = args.start or service.index.dates[0]
    end = args.end or latest(service)
    outcome = service.replay(start, end)
    print(f"{start} 〜 {end} をペーパーで再生しました（{outcome['days']}営業日、初回の約定 {outcome['first_fill']}）")
    return cmd_status(argparse.Namespace(profile=args.profile, date=end), service)


def cmd_audit(args, service: OperationsService) -> int:
    for row in service.ledger.audit_log(args.limit):
        print(f"{row['at']}  {row['key']}: {row['old']} → {row['new']}  ({row['reason']})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", choices=["paper", "live"], default="paper")
    parser.add_argument("--lot-size", type=int, default=1)
    parser.add_argument("--strategy", default="regime_hold")
    sub = parser.add_subparsers(dest="command", required=True)
    date = dt.date.fromisoformat

    p = sub.add_parser("init", help="戦略を凍結して始める")
    p.add_argument("--cash", type=float, required=True)
    p.add_argument("--param", action="append", default=[], help="例: --param ma=50")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("plan", help="売買候補を出す")
    p.add_argument("--as-of", type=date, default=None)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("fill", help="実際の約定を記録する")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", choices=["buy", "sell"], required=True)
    p.add_argument("--qty", type=float, required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--fee", type=float, default=0.0)
    p.add_argument("--date", type=date, default=None)
    p.add_argument("--signal-id", type=int, default=None)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_fill)

    p = sub.add_parser("mark", help="評価額を記録する")
    p.add_argument("--date", type=date, default=None)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("status", help="状況を見る")
    p.add_argument("--date", type=date, default=None)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("set-mode", help="実行モードを変える（0停止 1シグナル 2ペーパー）")
    p.add_argument("mode", type=int)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_set_mode)

    p = sub.add_parser("set-limit", help="リスク制限を変える（緩める変更は24時間後）")
    p.add_argument("item", nargs="+", help="例: max_order_value=3000")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_set_limit)

    p = sub.add_parser("kill", help="すべて止める")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("resume", help="再開する")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("replay", help="過去を1日ずつ進め、ペーパーで流れを回す")
    p.add_argument("--from", dest="start", type=date, default=None)
    p.add_argument("--to", dest="end", type=date, default=None)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("audit", help="設定変更の履歴")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_audit)

    args = parser.parse_args()
    service = build_service(args)
    try:
        if args.command != "init" and not service.initialized:
            raise OperationsError(f"未初期化です。先に: python scripts/ops.py --profile {args.profile} init --cash <金額>")
        return args.func(args, service)
    except StaleDataError as exc:
        print(f"候補を出せません: {exc}")
        return 2
    except OperationsError as exc:
        print(f"できません: {exc}")
        return 1
    finally:
        service.ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
