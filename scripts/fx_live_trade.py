"""FX の自動売買（**実際のお金**）。判断する者（AI・金利差＋流れ）が、実際の口座の状態を見て判断し、ここで発注する。

ペーパー運用（シミュレーション）とは独立。15分ごとの回（scripts/fx_paper.py）の中で、ペーパー運用を止めていても動く。

  * ``FX_LIVE_ENABLED=true`` のときだけ注文を出す（infra の ``fx_live_enabled``）。それ以外は、何をしたかを記録するだけ
  * キルスイッチ ``state/KILL_fx_live`` が在れば、新しく建てない（画面の「実際の約定」から入れられる）
  * 発注用のキー（gmo-fx-trade-api-key / secret）でだけ動く
  * 建てたら、すぐに GMOコイン側に損切り（と利確）を置く。置けなかったら、その建玉を成行で決済し、キルスイッチを入れる
  * 何をしたか・しなかったかは、すべて state/fx_live.jsonl に記録する
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 中身は部品（jqbt.interfaces.fx_live）にある。ここは互換のための入口
from jqbt.interfaces.fx_live import KILL, LiveSession, execute, fill_of, kill, time  # noqa: E402,F401
