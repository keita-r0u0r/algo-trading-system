"""実行モードと、増額の条件（純粋関数）。

## 実行モード

  0 停止       何もしない
  1 シグナル   売買候補を出す。発注は人が行い、約定を手で記録する
  2 ペーパー   仮想の約定で、実運用と同じ流れを回す
  3 確認付き / 4 自動   証券会社との連携が必要。**未実装**

下げるのはいつでも即時。上げるのは条件を満たしたときだけ。

## 増額の条件

資金を増やす前に、運用の結果が「想定どおり」かを数字で確かめる。
感覚で増やすと、偶然の勝ちに乗ってしまう。条件は ``GateThresholds`` で固定する。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Mode(IntEnum):
    STOP = 0
    SIGNAL = 1
    PAPER = 2
    CONFIRMED = 3
    AUTO = 4


#: 実装済みのモード。3以上は証券会社との連携が要る
IMPLEMENTED = {Mode.STOP, Mode.SIGNAL, Mode.PAPER}


def can_switch(current: Mode, target: Mode) -> tuple[bool, str]:
    """モードの切り替えが許されるか。"""
    if target not in IMPLEMENTED:
        return False, f"モード{int(target)}は未実装です（証券会社との連携が必要）"
    return True, ""


@dataclass(frozen=True)
class GateThresholds:
    min_days: int = 60
    min_fills: int = 30
    #: 実測スリッページが、想定の何倍までなら許容するか
    slippage_tolerance: float = 1.5
    #: 許容する最大ドローダウン（正の割合）
    max_drawdown: float = 0.15
    #: 日次損失の上限に触れた回数の許容
    max_loss_breaches: int = 0


@dataclass(frozen=True)
class LiveStats:
    days: int
    fills: int
    #: 不利な方向のずれの平均（比率）。計測できないときは None
    mean_slippage: float | None
    assumed_slippage: float
    #: 最大ドローダウン（負の割合）
    max_drawdown: float
    loss_breaches: int = 0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def evaluate(stats: LiveStats, thresholds: GateThresholds = GateThresholds()) -> list[Check]:
    """増額の条件を1つずつ判定する。全部満たすまで、次の段階へは進めない。"""
    checks = [
        Check(
            "継続日数",
            stats.days >= thresholds.min_days,
            f"{stats.days} / {thresholds.min_days}日",
        ),
        Check(
            "約定数",
            stats.fills >= thresholds.min_fills,
            f"{stats.fills} / {thresholds.min_fills}件",
        ),
    ]
    if stats.mean_slippage is None:
        checks.append(Check("スリッページ", False, "計測できる約定がない"))
    else:
        limit = stats.assumed_slippage * thresholds.slippage_tolerance
        checks.append(
            Check(
                "スリッページ",
                stats.mean_slippage <= limit,
                f"実測 {stats.mean_slippage:+.2%} / 許容 {limit:+.2%}（想定 {stats.assumed_slippage:.2%}）",
            )
        )
    checks.append(
        Check(
            "最大ドローダウン",
            stats.max_drawdown >= -thresholds.max_drawdown,
            f"{stats.max_drawdown:+.1%} / 許容 -{thresholds.max_drawdown:.0%}",
        )
    )
    checks.append(
        Check(
            "日次損失の上限",
            stats.loss_breaches <= thresholds.max_loss_breaches,
            f"{stats.loss_breaches}回 / 許容 {thresholds.max_loss_breaches}回",
        )
    )
    return checks


def all_passed(checks: list[Check]) -> bool:
    return bool(checks) and all(c.ok for c in checks)
