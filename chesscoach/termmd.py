"""Markdown → terminal rendering for the coach's reports.

The reports are written as Markdown files; when one is shown INSIDE the
terminal it should read like a document, not like markup. Pure stdlib:
headers get the gold accent, tables become aligned columns, links keep
their text (URL dimmed away), emphasis becomes ANSI. Piped output stays
raw markdown so redirects still produce a valid .md.
"""
from __future__ import annotations

import re
import sys

GOLD, BOLD, DIM, OFF = "\033[1;33m", "\033[1m", "\033[2m", "\033[0m"

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _width(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _inline(text: str) -> str:
    """Links keep their text, **bold** becomes ANSI bold."""
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} {DIM}({m.group(2)}){OFF}",
                        text)
    return _BOLD_RE.sub(lambda m: f"{BOLD}{m.group(1)}{OFF}", text)


def _flush_table(rows: list[list[str]], out: list[str]) -> None:
    if not rows:
        return
    cols = max(len(r) for r in rows)
    for r in rows:
        r += [""] * (cols - len(r))
    widths = [max(_width(r[i]) for r in rows) for i in range(cols)]
    header, *body = rows
    def line(cells, style=""):
        parts = [f"{style}{c}{OFF}" + " " * (widths[i] - _width(c))
                 for i, c in enumerate(cells)]
        return "   " + f"  {DIM}·{OFF}  ".join(parts).rstrip()
    out.append(line(header, BOLD))
    out.append(f"   {DIM}" + "─" * (sum(widths) + 5 * (cols - 1)) + OFF)
    out.extend(line(r) for r in body)


def render_markdown(md: str, color: bool | None = None) -> str:
    """The report as terminal text. Raw markdown when not a tty."""
    if color is None:
        color = sys.stdout.isatty()
    if not color:
        return md
    out: list[str] = []
    table: list[list[str]] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells):
                continue  # the |---|---| separator row
            table.append([_inline(c) for c in cells])
            continue
        _flush_table(table, out)
        table = []
        if stripped.startswith("### "):
            out.append(f"{BOLD}{stripped[4:]}{OFF}")
        elif stripped.startswith("## "):
            out.append(f"{GOLD}▍{stripped[3:]}{OFF}")
        elif stripped.startswith("# "):
            out.append(f"{GOLD}{BOLD}{stripped[2:].upper()}{OFF}")
        elif stripped.startswith("_") and stripped.endswith("_"):
            out.append(f"{DIM}{stripped[1:-1]}{OFF}")
        elif stripped.startswith("> "):
            out.append(f"   {DIM}▏{OFF}{_inline(stripped[2:])}")
        elif stripped.startswith("- "):
            out.append(f"  {GOLD}•{OFF} {_inline(stripped[2:])}")
        else:
            out.append(_inline(line))
    _flush_table(table, out)
    return "\n".join(out)
