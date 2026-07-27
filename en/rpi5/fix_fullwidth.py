# -*- coding: utf-8 -*-
"""英文版前端的**全形標點**清理（訪客看得到的位置）。

背景：英文化腳本 `make_en_ui.py` 是逐條字串替換，只換了「開頭」沒換「結尾」
      → 出現 `To remove, say "delete AL001」` 這種**中英混排引號**。
      這類殘留只有渲染出來才看得到，讀 JSON 或掃 innerText 都不明顯。

⚠️ 保守原則（第一版寫太粗暴，把整份 CSS 的對齊排版都毀了、111 行）：
   ①**只碰含全形標點的行**，其餘一字不動
   ②**不做空白正規化**（那是 CSS 對齊，不是我們的事）
   ③`·`（間隔號）保留——中英文都是合法分隔符
   ④跳過註解行與 `.rel-quip::` 那種刻意的引號樣式
"""
import sys
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "en/templates/index.html")
DRY = "--apply" not in sys.argv

PAIRS = [
    ("「", '"'), ("」", '"'),
    ("『", "'"), ("』", "'"),
    ("（", "("), ("）", ")"),
    ("：", ": "), ("；", "; "),
    ("、", ", "), ("。", ". "),
    ("？", "?"), ("！", "!"),
]
FW = {fw for fw, _ in PAIRS}

lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)
out, changed = [], []
for n, ln in enumerate(lines, 1):
    s = ln.lstrip()
    if (not (FW & set(ln))
            or s.startswith(("//", "/*", "*", "#"))
            or ".rel-quip::" in ln):
        out.append(ln)
        continue
    new = ln
    for fw, hw in PAIRS:
        new = new.replace(fw, hw)
    if new != ln:
        changed.append((n, ln.rstrip()[:88], new.rstrip()[:88]))
    out.append(new)

print(f"{'（預覽）' if DRY else '（已寫入）'} 共 {len(changed)} 行")
for n, a, b in changed:
    print(f"  L{n}\n    - {a}\n    + {b}")
if changed and not DRY:
    SRC.write_text("".join(out), encoding="utf-8")
