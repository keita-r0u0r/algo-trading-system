"""jqbt — 国内株のバックテスト環境（研究用）。

## 層

    interfaces   → application → core
   （CLI）        （手続き）     （純粋関数）
                       ↑
                 infrastructure
              （Port の実装を注入する）

``core`` は他のどの層も import しない。``application`` は自分が定義した
Port にだけ依存し、``infrastructure`` を知らない。組み立ては
``interfaces`` だけが行う。``tests/test_architecture.py`` が機械的に検査する。

## 設計方針

**先読みを構造的に不可能にする。** それ以外は必要になってから足す。
"""

from .core.contracts import (
    Bar,
    BacktestResult,
    DailySnapshot,
    Fill,
    Order,
    Portfolio,
    Position,
    RejectReason,
    Rejection,
    RoundTrip,
    Side,
)
from .core.costs import (
    LOT_SIZE,
    OPTIMISTIC,
    PESSIMISTIC,
    REALISTIC,
    CostModel,
    tick_size,
)
from .core.metrics import Metrics, compute, format_report

__all__ = [
    # contracts
    "Bar",
    "BacktestResult",
    "DailySnapshot",
    "Fill",
    "Order",
    "Portfolio",
    "Position",
    "RejectReason",
    "Rejection",
    "RoundTrip",
    "Side",
    # costs
    "CostModel",
    "OPTIMISTIC",
    "REALISTIC",
    "PESSIMISTIC",
    "LOT_SIZE",
    "tick_size",
    # metrics
    "Metrics",
    "compute",
    "format_report",
]
