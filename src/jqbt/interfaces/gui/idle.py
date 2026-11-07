"""操作がない画面の接続を切る。

Streamlit の画面は、開いている間ずっと接続を保つ。Cloud Run は接続がある間を課金するので、開きっぱなしのタブが費用になる。
一定の時間操作がなければ、案内のページ（static/idle.html）へ移って接続を切る（「開き直す」で戻れる）。

自動更新を選んで画面を前に出している間は、見ている途中なので切らない。隠れているタブは、自動更新を選んでいても切る。
"""

from __future__ import annotations

import json

IDLE_MINUTES = 10
EVENTS = ("pointerdown", "pointermove", "keydown", "wheel", "touchstart", "scroll")
CHECK_MS = 15_000

# 親のページで動かす本体（1度だけ入れる）。部品の枠の中の処理は、描き直しで作り直されるので、そこには置かない
_WATCHER = """
(() => {
  let last = Date.now();
  const touch = () => { last = Date.now(); };
  EVENTS.forEach((name) => addEventListener(name, touch, {capture: true, passive: true}));
  setInterval(() => {
    if (window.__jqbtAutoRefresh && !document.hidden) { touch(); return; }
    if (Date.now() - last < (window.__jqbtIdleMs || IDLE_MS)) return;
    location.assign("/app/static/idle.html");
  }, CHECK_MS);
})();
"""


def idle_script(auto_refresh: bool, minutes: int = IDLE_MINUTES) -> str:
    """画面に埋め込む HTML。``auto_refresh`` は描き直しのたびに親のページへ伝える。"""
    watcher = (_WATCHER.replace("EVENTS", json.dumps(EVENTS))
               .replace("IDLE_MS", str(minutes * 60_000)).replace("CHECK_MS", str(CHECK_MS)))
    return f"""<script>
const parentWindow = window.parent;
parentWindow.__jqbtAutoRefresh = {json.dumps(auto_refresh)};
if (!parentWindow.__jqbtIdleWatcher) {{
  parentWindow.__jqbtIdleWatcher = true;
  const element = parentWindow.document.createElement("script");
  element.textContent = {json.dumps(watcher).replace("</", "<\\/")};
  parentWindow.document.head.appendChild(element);
}}
</script>"""
