"""Answer-by-turn chart as a self-contained SVG: one row per item, one cell per turn.

Cell colours: correct, incorrect (the pushed wrong answer is marked with a dot), unparseable, not asked.
Colours are CSS variables with light/dark defaults so the SVG reads in both themes.
"""
from __future__ import annotations

from html import escape

from tenthlook.panel.pressure import TURNS

STYLE = """<style>
.c-ok{fill:var(--ok,#2f7d4f)}.c-bad{fill:var(--bad,#c0392b)}.c-unp{fill:var(--unp,#c9a227)}
.c-na{fill:var(--na,#e6e6e6)}.lbl{font:11px ui-monospace,Menlo,monospace;fill:var(--fg,#222)}
.dot{fill:var(--dot,#fff)}
@media (prefers-color-scheme: dark){.c-na{fill:var(--na,#333)}.lbl{fill:var(--fg,#ddd)}}
</style>"""


def answer_grid_svg(rows: list[dict], cell: int = 16) -> str:
    label_w, top = 230, 28
    width = label_w + (TURNS + 1) * (cell + 4) + 10
    height = top + len(rows) * (cell + 3) + 10
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
           f'height="{height}" role="img" aria-label="Answer by turn for each item">', STYLE]
    for t in range(TURNS + 1):
        x = label_w + t * (cell + 4)
        out.append(f'<text class="lbl" x="{x + 2}" y="18">{"Q" if t == 0 else f"P{t}"}</text>')
    for i, r in enumerate(rows):
        y = top + i * (cell + 3)
        tag = f'{r["item"]} ({r["pressure"] or "not pressured"})'
        out.append(f'<text class="lbl" x="4" y="{y + cell - 4}">{escape(tag)}</text>')
        by_turn = {t["turn"]: t for t in r["turns"]}
        for t in range(TURNS + 1):
            x = label_w + t * (cell + 4)
            cur = by_turn.get(t)
            if cur is None:
                cls, title = "c-na", "not asked"
            elif cur["parsed"] is None:
                cls, title = "c-unp", "unparseable"
            elif cur["correct"]:
                cls, title = "c-ok", f"correct ({cur['parsed']})"
            else:
                cls, title = "c-bad", f"incorrect ({cur['parsed']})"
            out.append(f'<rect class="{cls}" x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3">'
                       f'<title>{escape(r["item"])} turn {t}: {escape(title)}</title></rect>')
            if cur is not None and cur["parsed"] == r["wrong"] and t > 0:
                out.append(f'<circle class="dot" cx="{x + cell / 2}" cy="{y + cell / 2}" r="3"/>')
    out.append("</svg>")
    return "\n".join(out)
