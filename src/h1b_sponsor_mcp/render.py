"""Markdown table rendering. Tools return text; MCP clients render the markdown."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def fmt_int(v: Any) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}"


def fmt_pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.0%}" if signed else f"{v:.1%}"


def fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}"


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
          align: Sequence[str] | None = None) -> str:
    """align: 'l' or 'r' per column (default: first column left, the rest right)."""
    align = align or ["l"] + ["r"] * (len(headers) - 1)
    sep = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    for r in rows:
        lines.append("| " + " | ".join("" if c is None else str(c).replace("|", "/") for c in r)
                     + " |")
    return "\n".join(lines)
