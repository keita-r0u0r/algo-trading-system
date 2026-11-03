"""ハーネス（``fx_features``）の数字だけで売買を決める決まり（I/O なし）。判断する者 ``harness`` が使う。

AI に売買の芯を任せない形。建てるか・向き・損切り・利確・決済は、ここの決まりが毎回同じ数字から決める。
AI や Jev ができるのは、決まりが出した候補を「見送るか」と、用意した決済の手法（``fx_exits``）のどれを使うかを選ぶことだけ
（``fx_filter``）。向き・数量・損切りは変えられない。

## 建てる（流れの中の押し目だけ）

  1. 流れ: 4時間の動きがふだんの揺れの ``trend_ratio`` 倍以上、24時間の動きも同じ向き、1時間足の50本平均も同じ向き
  2. 押し目: 5分足が20本の平均から、流れと逆に ``pullback_z`` σ以上戻っている（流れの途中を追いかけない）
  3. 戻りの終わり: 1分足の20本平均が、流れの向きに戻り始めている
  4. 重要な経済指標の前後でない。同じ通貨ペア・同じ向きで損をしてから ``reentry_block_minutes`` たっている
  5. 損切りは1時間足のふだんの揺れの ``stop_atr`` 倍（``min_stop_pips``〜``max_stop_pips``）

「買われすぎだから下がる」（流れに逆らう取引）はしない。強い流れの中の RSI の高止まりは勢いの印で、反転の根拠にならない。

## 決済（建てたあとは機械が守る）

  * 最初の損切りは GMOコイン側に置く（システムが止まっても効く）
  * そのあとの降りる線は、建てるときに選んだ決済の手法（``fx_exits``）で決める。選んだ手法は記録に残す
  * 流れが逆を向いたら決済する（建てた根拠が崩れた）
  * ``hold_minutes`` たっても損切り幅の分まで伸びなければ決済する（時間切れ）
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any, Iterable

import pandas as pd

from . import fx_exits

#: 決まりを変えたら上げる（記録を版ごとに分けて比べるため）
HARNESS_VERSION = "harness-v2"
PIP = 0.01
SETUP_LABELS = {"pullback": "流れの中の押し目"}


@dataclass(frozen=True)
class HarnessRules:
    trend_ratio: float = 1.0
    pullback_z: float = 0.5
    stop_atr: float = 0.8
    min_stop_pips: float = 12.0
    max_stop_pips: float = 40.0
    hold_minutes: int = 360
    reentry_block_minutes: int = 120
    risk_fraction: float = 0.01
    #: 決済の手法を選ばない（または選べなかった）ときに使う手法
    exit_method: str = "chandelier"
    exits: fx_exits.ExitParams = fx_exits.ExitParams()

    def describe(self) -> str:
        return (f"流れ（4時間の動きが揺れの{self.trend_ratio:g}倍以上・24時間と1時間足の平均も同じ向き）の中で、"
                f"5分足が{self.pullback_z:g}σ以上押して1分足が戻り始めたら建てる。損切りは1時間足の揺れの{self.stop_atr:g}倍"
                f"（{self.min_stop_pips:g}〜{self.max_stop_pips:g} pips）。"
                f"決済は建てるときに選んだ手法（決まった手法は {self.exit_method}）。"
                f"流れが逆を向いたら決済、{self.hold_minutes // 60}時間で損切り幅の分まで伸びなければ決済。"
                f"重要な指標の前後と、同じ向きで損をしてから{self.reentry_block_minutes}分は建てない")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: int  # 1 = 買い、-1 = 売り
    setup: str
    price: float
    stop_pips: float
    invalidation_price: float
    hold_minutes: int
    strength: float
    atr_pips: float
    evidence: list[str]

    def decision(self, rules: HarnessRules, method: str | None = None) -> dict[str, Any]:
        """枠（core.fx.frame）に通す判断の形。利確は決済の手法で決まる（追いかける手法は置かない）。"""
        method = method if method in fx_exits.METHODS else rules.exit_method
        take = round(self.stop_pips * rules.exits.take_for(method), 1)
        return {"action": "open_long" if self.side > 0 else "open_short", "symbol": self.symbol,
                "risk_fraction": rules.risk_fraction, "stop_pips": self.stop_pips, "take_pips": take,
                "setup": self.setup, "exit_method": method, "invalidation_price": self.invalidation_price,
                "target_price": round(self.price + self.side * take * PIP, 3) if take else None,
                "hold_minutes": self.hold_minutes,
                "reasoning": " / ".join(self.evidence) + f" / 決済の手法: {fx_exits.METHODS[method]}"}


def _get(f: dict, *path: str) -> float | None:
    for key in path:
        if not isinstance(f, dict) or key not in f:
            return None
        f = f[key]
    return f if isinstance(f, (int, float)) else None


def trend(f: dict[str, Any], rules: HarnessRules = HarnessRules()) -> tuple[int, list[str]]:
    """流れの向き（1・-1・0）と、その根拠の文。"""
    r4, c24 = _get(f, "moves", "4h", "noise_ratio"), _get(f, "moves", "24h", "change_pips")
    slope = _get(f, "timeframes", "1h", "ema50_slope_pips")
    if r4 is None or c24 is None or slope is None:
        return 0, ["流れを測る足が足りない"]
    side = 1 if r4 > 0 else -1
    facts = f"4時間の動きは揺れの{r4:+.2f}倍、24時間 {c24:+.1f} pips、1時間足の50本平均の向き {slope:+.2f} pips/本"
    if abs(r4) >= rules.trend_ratio and side * c24 > 0 and side * slope > 0:
        return side, [f"{'上' if side > 0 else '下'}向きの流れ（{facts}）"]
    return 0, [f"流れがはっきりしない（{facts}）"]


def entry(symbol: str, f: dict[str, Any], *, quiet: str | None = None, blocked: str | None = None,
          rules: HarnessRules = HarnessRules()) -> tuple[Candidate | None, list[str]]:
    """1つの通貨ペアで建てる候補（無ければ None）と、見た根拠・見送った理由。"""
    side, why = trend(f, rules)
    if side == 0:
        return None, why
    z = _get(f, "timeframes", "5min", "bollinger_z")
    turn = _get(f, "timeframes", "1min", "ema20_slope_pips")
    atr_1h = _get(f, "timeframes", "1h", "atr_pips")
    price = _get(f, "price")
    if z is None or turn is None or atr_1h is None or price is None:
        return None, why + ["押し目を測る足が足りない"]
    if side * z > -rules.pullback_z:
        return None, why + [f"押していない（5分足は平均から {z:+.2f}σ。流れの途中を追わない）"]
    why.append(f"5分足が平均から {z:+.2f}σ 押した")
    if side * turn <= 0:
        return None, why + [f"押しがまだ止まっていない（1分足の平均の向き {turn:+.2f} pips/本）"]
    why.append(f"1分足が戻り始めた（平均の向き {turn:+.2f} pips/本）")
    if quiet:
        return None, why + [f"重要な指標の前後: {quiet}"]
    if blocked:
        return None, why + [blocked]
    stop = round(min(rules.max_stop_pips, max(rules.min_stop_pips, rules.stop_atr * atr_1h)), 1)
    why.append(f"損切り {stop:g} pips（1時間足の揺れ {atr_1h:g} pips の{rules.stop_atr:g}倍）")
    r4 = _get(f, "moves", "4h", "noise_ratio") or 0.0
    return Candidate(symbol, side, "pullback", price, stop, round(price - side * stop * PIP, 3),
                     rules.hold_minutes, abs(r4), atr_1h, why), why


def best_entry(features: dict[str, dict[str, Any]], *, quiet: dict[str, str | None] | None = None,
               blocked: dict[str, dict[int, str]] | None = None, rules: HarnessRules = HarnessRules()
               ) -> tuple[Candidate | None, dict[str, list[str]]]:
    """全部の通貨ペアの候補から、流れのいちばん強いもの。通貨ペアごとの根拠も返す。"""
    seen: dict[str, list[str]] = {}
    found: list[Candidate] = []
    for sym, f in features.items():
        if not f:
            continue
        side, _ = trend(f, rules)
        c, why = entry(sym, f, quiet=(quiet or {}).get(sym), blocked=(blocked or {}).get(sym, {}).get(side),
                       rules=rules)
        seen[sym] = why
        if c is not None:
            found.append(c)
    return (max(found, key=lambda c: c.strength) if found else None), seen


@dataclass(frozen=True)
class ExitCheck:
    close: bool
    reason: str
    peak_pips: float
    unrealized_pips: float
    #: 見張り役がこの値段を越えたら起こす（決済の手法の降りる線。まだ無ければ None）
    exit_level: float | None
    evidence: list[str]


def since_open(bars: pd.DataFrame, opened_at: dt.datetime) -> pd.DataFrame:
    """建ててから今までの1分足。"""
    return bars[bars.index >= pd.Timestamp(opened_at.replace(tzinfo=None)).floor("min")]


def peak_pips(bars: pd.DataFrame, side: int, entry_price: float, opened_at: dt.datetime) -> float:
    """建ててから今までの、いちばん有利だったときの含み益（pips。1分足の高値・安値）。"""
    since = since_open(bars, opened_at)
    if since.empty:
        return 0.0
    best = float(since["high"].max()) if side > 0 else float(since["low"].min())
    return max(0.0, side * (best - entry_price) / PIP)


def exit_check(*, side: int, entry_price: float, stop_pips: float, price: float, bars: pd.DataFrame | None,
               opened_at: dt.datetime, now: dt.datetime, f: dict[str, Any] | None, method: str | None = None,
               rules: HarnessRules = HarnessRules()) -> ExitCheck:
    """建玉を、決まりで閉じるか。``price`` は今の値段（仲値）、``bars`` は1分足（建てる前を含んでよい）。"""
    method = method if method in fx_exits.METHODS else rules.exit_method
    since = since_open(bars, opened_at) if bars is not None else pd.DataFrame()
    unrealized = side * (price - entry_price) / PIP
    peak = max(unrealized, peak_pips(bars, side, entry_price, opened_at) if bars is not None else 0.0)
    evidence = [f"決済の手法 {method}。含み損益 {unrealized:+.1f} pips（最大 {peak:+.1f}）、損切り幅 {stop_pips:g} pips"]
    level = fx_exits.exit_level(method, side=side, entry=entry_price, stop_pips=stop_pips, since_open=since,
                                atr_pips=_get(f or {}, "timeframes", "1h", "atr_pips"), params=rules.exits)
    if level is not None:
        evidence.append(f"降りる線 {level}（{side * (level - entry_price) / PIP:+.1f} pips）")
        if side * (price - level) <= 0:
            return ExitCheck(True, f"決済の手法（{fx_exits.METHODS[method]}）の線 {level} を割った"
                                   f"（最大 {peak:+.1f} pips → 今 {unrealized:+.1f} pips）", peak, unrealized, level, evidence)
    if f:
        now_side, why = trend(f, rules)
        evidence += why
        if now_side == -side:
            return ExitCheck(True, "流れが逆を向いた（建てた根拠が崩れた）: " + why[0], peak, unrealized, level, evidence)
    elapsed = (now - opened_at).total_seconds() / 60
    if elapsed >= rules.hold_minutes and peak < stop_pips:
        return ExitCheck(True, f"時間切れ: {elapsed / 60:.1f}時間たっても損切り幅の分まで伸びなかった",
                         peak, unrealized, level, evidence)
    return ExitCheck(False, "", peak, unrealized, level, evidence)


def closed_trades(events: Iterable[dict[str, Any]], account: str) -> list[dict[str, Any]]:
    """実際の口座の記録から、判断する者 ``account`` の決済した取引（通貨・向き・損益・決済の時刻）。"""
    opens: dict[int, dict] = {}
    pnl: dict[int, float] = {}
    closed_at: dict[int, str] = {}
    for e in events:
        kind = e.get("kind")
        if kind == "live_open" and e.get("account") == account and e.get("position_id"):
            opens[int(e["position_id"])] = e
        elif kind == "execution" and e.get("settleType") == "CLOSE" and e.get("positionId") is not None:
            pid = int(e["positionId"])
            pnl[pid] = pnl.get(pid, 0.0) + float(e.get("lossGain") or 0)
            closed_at[pid] = str(e.get("at", ""))
    return [{"symbol": o["symbol"], "side": 1 if o["side"] == "BUY" else -1, "pnl": round(pnl[pid], 1),
             "opened_at": o.get("at", ""), "closed_at": closed_at[pid]}
            for pid, o in opens.items() if pid in pnl]


def reentry_blocks(trades: list[dict[str, Any]], now: dt.datetime, rules: HarnessRules = HarnessRules()
                   ) -> dict[str, dict[int, str]]:
    """同じ通貨ペア・同じ向きで損をしてから間もないもの（{通貨: {向き: 理由}}）。"""
    blocks: dict[str, dict[int, str]] = {}
    for t in trades:
        if t["pnl"] >= 0 or not t["closed_at"]:
            continue
        minutes = (now - dt.datetime.fromisoformat(t["closed_at"])).total_seconds() / 60
        if 0 <= minutes < rules.reentry_block_minutes:
            blocks.setdefault(t["symbol"], {})[t["side"]] = (
                f"同じ向きで {minutes:.0f}分前に損をした（{t['pnl']:+.0f}円）。{rules.reentry_block_minutes}分は同じ向きに建てない")
    return blocks


def rules_record(rules: HarnessRules = HarnessRules()) -> dict[str, Any]:
    return {"rule": rules.describe(), "version": HARNESS_VERSION, "params": asdict(rules)}
