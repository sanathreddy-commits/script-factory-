"""Production runs on Supabase Postgres, tests on SQLite. Catch SQL that only SQLite accepts.

Postgres cannot SUM/AVG a boolean: SUM(status='X') or SUM(EXISTS(...)) fails there with
"function sum(boolean) does not exist" (this broke script assignment). Use
SUM(CASE WHEN ... THEN 1 ELSE 0 END) instead.
"""
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_AGG = re.compile(r"\b(SUM|AVG|TOTAL)\(")  # SQL is written upper-case; skips Python sum()
_BOOLISH = re.compile(r"(?<![<>!])=(?!=)|<>|!=|\bIN\s*\(|\bEXISTS\s*\(|\bLIKE\b|\bIS\s+(NOT\s+)?NULL\b|\bAND\b|\bOR\b", re.I)


def _arg(text, start):
    """Text inside the parentheses that open at text[start-1]."""
    depth, i = 1, start
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1]


def test_no_boolean_aggregates_in_sql():
    bad = []
    for path in APP.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for m in _AGG.finditer(src):
            arg = _arg(src, m.end())
            head = arg.lstrip().upper()
            if head.startswith("CASE") or head.startswith("COALESCE"):
                continue
            # only the top level of the argument matters; nested CASE/subqueries are fine
            if _BOOLISH.search(re.sub(r"\([^()]*\)", "", arg)) or re.match(r"\s*(NOT\s+)?EXISTS\s*\(", arg, re.I):
                line = src[:m.start()].count("\n") + 1
                bad.append(f"{path.relative_to(APP.parent)}:{line}: {m.group(0)}{arg[:70]}")
    assert not bad, "Boolean aggregate fails on Postgres; use SUM(CASE WHEN ... THEN 1 ELSE 0 END):\n" + "\n".join(bad)
